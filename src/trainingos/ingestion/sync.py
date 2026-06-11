"""Incremental, resumable, and auditable source synchronization."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar
from uuid import uuid4

PayloadT = TypeVar("PayloadT")


class SyncDisposition(StrEnum):
    IMPORTED = "imported"
    SKIPPED = "skipped"


class SyncStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SyncError(RuntimeError):
    """A source or handler error whose message is safe to persist."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if not code or not code.strip():
            raise ValueError("sync error code must not be blank")
        if not message or not message.strip():
            raise ValueError("sync error message must not be blank")
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SyncProtocolError(SyncError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_adapter_response", message)


@dataclass(frozen=True, slots=True)
class SyncRecord(Generic[PayloadT]):
    """A source record paired with its non-regressing high-water cursor.

    ``cursor_after`` becomes the durable checkpoint after this record commits.
    It must be at or after the prior cursor for the source, including when the
    adapter returns records from an overlapping window.
    """

    external_id: str
    cursor_after: str
    payload: PayloadT

    def __post_init__(self) -> None:
        if not self.external_id or not self.external_id.strip():
            raise ValueError("external_id must not be blank")
        if not self.cursor_after or not self.cursor_after.strip():
            raise ValueError("cursor_after must not be blank")


@dataclass(frozen=True, slots=True)
class SyncPage(Generic[PayloadT]):
    records: tuple[SyncRecord[PayloadT], ...]
    done: bool


class SourceAdapter(Protocol[PayloadT]):
    @property
    def source(self) -> str: ...

    def fetch(self, cursor: str | None, limit: int) -> SyncPage[PayloadT]: ...

    def cursor_is_at_or_after(self, previous: str, candidate: str) -> bool:
        """Return whether candidate is a non-regressing source checkpoint."""
        ...


class RecordHandler(Protocol[PayloadT]):
    def __call__(
        self,
        connection: sqlite3.Connection,
        record: SyncRecord[PayloadT],
    ) -> SyncDisposition: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class SyncOptions:
    batch_size: int = 100
    dry_run: bool = False
    retry: RetryPolicy = RetryPolicy()

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")


@dataclass(frozen=True, slots=True)
class SyncReport:
    sync_run_id: str
    source: str
    status: SyncStatus
    cursor_start: str | None
    cursor_end: str | None
    imported_count: int
    skipped_count: int
    failed_count: int
    dry_run: bool


class SyncRunner:
    """Runs one source sequentially and advances checkpoints after durable writes."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._connection = connection
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper

    def run(
        self,
        adapter: SourceAdapter[PayloadT],
        handler: RecordHandler[PayloadT],
        options: SyncOptions | None = None,
    ) -> SyncReport:
        selected_options = options or SyncOptions()
        source = adapter.source
        if not source or not source.strip():
            raise ValueError("adapter source must not be blank")
        if self._connection.in_transaction:
            raise RuntimeError("sync runner requires a connection with no active transaction")

        cursor_start = self._load_checkpoint(source)
        run_id = str(uuid4())
        self._start_run(run_id, source, cursor_start, selected_options.dry_run)
        cursor = cursor_start

        try:
            while True:
                page = self._fetch_page(
                    run_id, adapter, cursor, selected_options
                )
                if page is None:
                    return self._finish(run_id, SyncStatus.FAILED)
                if not page.records and not page.done:
                    self._record_terminal_error(
                        run_id,
                        None,
                        SyncProtocolError(
                            "an unfinished page must contain at least one record"
                        ),
                        1,
                    )
                    return self._finish(run_id, SyncStatus.FAILED)

                for record in page.records:
                    if cursor is not None and not self._validate_cursor(
                        run_id,
                        adapter,
                        cursor,
                        record,
                    ):
                        return self._finish(run_id, SyncStatus.FAILED)
                    processed = self._process_record(
                        run_id,
                        source,
                        record,
                        handler,
                        selected_options,
                    )
                    if not processed:
                        return self._finish(run_id, SyncStatus.FAILED)
                    cursor = record.cursor_after

                if page.done:
                    return self._finish(run_id, SyncStatus.COMPLETED)
        except KeyboardInterrupt:
            if self._connection.in_transaction:
                self._connection.rollback()
            self._finish(run_id, SyncStatus.CANCELLED)
            raise

    def _validate_cursor(
        self,
        run_id: str,
        adapter: SourceAdapter[PayloadT],
        previous: str,
        record: SyncRecord[PayloadT],
    ) -> bool:
        try:
            if adapter.cursor_is_at_or_after(previous, record.cursor_after):
                return True
            error = SyncProtocolError(
                "record cursor regressed relative to the current checkpoint"
            )
        except Exception as exception:
            error = _public_error(exception)
        self._record_terminal_error(run_id, record.external_id, error, 1)
        return False

    def _fetch_page(
        self,
        run_id: str,
        adapter: SourceAdapter[PayloadT],
        cursor: str | None,
        options: SyncOptions,
    ) -> SyncPage[PayloadT] | None:
        for attempt in range(1, options.retry.max_attempts + 1):
            try:
                return adapter.fetch(cursor, options.batch_size)
            except Exception as error:
                sync_error = _public_error(error)
                self._record_error(run_id, None, sync_error, attempt)
                if not sync_error.retryable or attempt == options.retry.max_attempts:
                    self._increment_failed(run_id)
                    return None
                self._sleeper(options.retry.backoff_seconds * attempt)
        return None

    def _process_record(
        self,
        run_id: str,
        source: str,
        record: SyncRecord[PayloadT],
        handler: RecordHandler[PayloadT],
        options: SyncOptions,
    ) -> bool:
        for attempt in range(1, options.retry.max_attempts + 1):
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                disposition = handler(self._connection, record)
                if disposition not in (
                    SyncDisposition.IMPORTED,
                    SyncDisposition.SKIPPED,
                ):
                    raise SyncProtocolError(
                        "record handler returned an unsupported disposition"
                    )

                if options.dry_run:
                    self._connection.rollback()
                    self._record_dry_run_success(
                        run_id, record.cursor_after, disposition
                    )
                else:
                    self._advance_checkpoint(
                        run_id,
                        source,
                        record.cursor_after,
                        disposition,
                    )
                    self._connection.commit()
                return True
            except Exception as error:
                if self._connection.in_transaction:
                    self._connection.rollback()
                sync_error = _public_error(error)
                self._record_error(
                    run_id, record.external_id, sync_error, attempt
                )
                if not sync_error.retryable or attempt == options.retry.max_attempts:
                    self._increment_failed(run_id)
                    return False
                self._sleeper(options.retry.backoff_seconds * attempt)
        return False

    def _load_checkpoint(self, source: str) -> str | None:
        row = self._connection.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = ?",
            (source,),
        ).fetchone()
        return None if row is None else row["cursor"]

    def _start_run(
        self,
        run_id: str,
        source: str,
        cursor_start: str | None,
        dry_run: bool,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, cursor_start, dry_run
            ) VALUES (?, ?, 'running', ?, ?, ?)
            """,
            (run_id, source, self._timestamp(), cursor_start, int(dry_run)),
        )
        self._connection.commit()

    def _advance_checkpoint(
        self,
        run_id: str,
        source: str,
        cursor: str,
        disposition: SyncDisposition,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO sync_checkpoints (source, cursor, updated_at, sync_run_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (source) DO UPDATE SET
                cursor = excluded.cursor,
                updated_at = excluded.updated_at,
                sync_run_id = excluded.sync_run_id
            """,
            (source, cursor, self._timestamp(), run_id),
        )
        count_column = _count_column(disposition)
        self._connection.execute(
            f"""
            UPDATE sync_runs
            SET cursor_end = ?, {count_column} = {count_column} + 1
            WHERE sync_run_id = ?
            """,
            (cursor, run_id),
        )

    def _record_dry_run_success(
        self,
        run_id: str,
        cursor: str,
        disposition: SyncDisposition,
    ) -> None:
        count_column = _count_column(disposition)
        self._connection.execute(
            f"""
            UPDATE sync_runs
            SET cursor_end = ?, {count_column} = {count_column} + 1
            WHERE sync_run_id = ?
            """,
            (cursor, run_id),
        )
        self._connection.commit()

    def _record_error(
        self,
        run_id: str,
        external_id: str | None,
        error: SyncError,
        attempt: int,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO sync_errors (
                sync_run_id, external_id, error_code, message, occurred_at,
                attempt, retryable
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                external_id,
                error.code,
                str(error),
                self._timestamp(),
                attempt,
                int(error.retryable),
            ),
        )
        self._connection.commit()

    def _record_terminal_error(
        self,
        run_id: str,
        external_id: str | None,
        error: SyncError,
        attempt: int,
    ) -> None:
        self._record_error(run_id, external_id, error, attempt)
        self._increment_failed(run_id)

    def _increment_failed(self, run_id: str) -> None:
        self._connection.execute(
            """
            UPDATE sync_runs
            SET failed_count = failed_count + 1
            WHERE sync_run_id = ?
            """,
            (run_id,),
        )
        self._connection.commit()

    def _finish(self, run_id: str, status: SyncStatus) -> SyncReport:
        self._connection.execute(
            """
            UPDATE sync_runs
            SET status = ?, finished_at = ?
            WHERE sync_run_id = ?
            """,
            (status.value, self._timestamp(), run_id),
        )
        self._connection.commit()
        row = self._connection.execute(
            """
            SELECT sync_run_id, source, status, cursor_start, cursor_end,
                   imported_count, skipped_count, failed_count, dry_run
            FROM sync_runs
            WHERE sync_run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return SyncReport(
            sync_run_id=row["sync_run_id"],
            source=row["source"],
            status=SyncStatus(row["status"]),
            cursor_start=row["cursor_start"],
            cursor_end=row["cursor_end"],
            imported_count=row["imported_count"],
            skipped_count=row["skipped_count"],
            failed_count=row["failed_count"],
            dry_run=bool(row["dry_run"]),
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sync clock must return a timezone-aware datetime")
        return value.astimezone(UTC).isoformat()


def _count_column(disposition: SyncDisposition) -> str:
    if disposition is SyncDisposition.IMPORTED:
        return "imported_count"
    return "skipped_count"


def _public_error(error: Exception) -> SyncError:
    if isinstance(error, SyncError):
        return error
    return SyncError(
        "unexpected_error",
        f"unexpected {type(error).__name__} during sync",
    )
