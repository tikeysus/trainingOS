from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trainingos.ingestion import (
    RetryPolicy,
    SyncDisposition,
    SyncError,
    SyncOptions,
    SyncPage,
    SyncRecord,
    SyncRunner,
    SyncStatus,
)
from trainingos.storage import apply_migrations, connect_database


@dataclass(frozen=True, slots=True)
class FixturePayload:
    value: str


class FakeAdapter:
    source = "fixture"

    def __init__(
        self,
        records: tuple[SyncRecord[FixturePayload], ...],
        *,
        overlap: bool = False,
        regress_overlap_cursor: bool = False,
    ) -> None:
        self.records = records
        self.overlap = overlap
        self.regress_overlap_cursor = regress_overlap_cursor
        self.fetch_calls: list[str | None] = []
        self.failures_remaining = 0

    def fetch(
        self,
        cursor: str | None,
        limit: int,
    ) -> SyncPage[FixturePayload]:
        self.fetch_calls.append(cursor)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise SyncError("source_unavailable", "fixture unavailable", retryable=True)

        start = 0 if self.overlap or cursor is None else int(cursor)
        selected = self.records[start : start + limit]
        if self.overlap and cursor is not None and not self.regress_overlap_cursor:
            selected = tuple(
                SyncRecord(
                    external_id=record.external_id,
                    cursor_after=cursor,
                    payload=record.payload,
                )
                for record in selected
            )
        done = start + len(selected) >= len(self.records)
        return SyncPage(records=selected, done=done)

    def cursor_is_at_or_after(self, previous: str, candidate: str) -> bool:
        return int(candidate) >= int(previous)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        self.connection = connect_database(database_path)
        apply_migrations(self.connection)
        self.connection.execute(
            """
            CREATE TABLE fixture_imports (
                external_id TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.commit()
        self.records = tuple(
            SyncRecord(
                external_id=f"activity-{index}",
                cursor_after=str(index),
                payload=FixturePayload(value=f"value-{index}"),
            )
            for index in range(1, 4)
        )
        self.sleeps: list[float] = []
        self.runner = SyncRunner(
            self.connection,
            clock=lambda: datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
            sleeper=self.sleeps.append,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_completed_run_persists_counts_checkpoint_and_audit_data(self) -> None:
        report = self.runner.run(
            FakeAdapter(self.records),
            self._idempotent_handler,
            SyncOptions(batch_size=2),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(3, report.imported_count)
        self.assertEqual(0, report.skipped_count)
        self.assertEqual("3", report.cursor_end)
        self.assertEqual(
            ("3", report.sync_run_id),
            tuple(
                self.connection.execute(
                    """
                    SELECT cursor, sync_run_id
                    FROM sync_checkpoints
                    WHERE source = 'fixture'
                    """
                ).fetchone()
            ),
        )
        self.assertEqual(3, self._import_count())

    def test_failed_record_rolls_back_and_next_run_resumes_from_checkpoint(self) -> None:
        def fail_on_second(
            connection: sqlite3.Connection,
            record: SyncRecord[FixturePayload],
        ) -> SyncDisposition:
            connection.execute(
                "INSERT INTO fixture_imports VALUES (?, ?)",
                (record.external_id, record.payload.value),
            )
            if record.external_id == "activity-2":
                raise SyncError("invalid_fixture", "fixture rejected")
            return SyncDisposition.IMPORTED

        first = self.runner.run(
            FakeAdapter(self.records),
            fail_on_second,
            SyncOptions(retry=RetryPolicy(max_attempts=1)),
        )
        second_adapter = FakeAdapter(self.records)
        second = self.runner.run(second_adapter, self._idempotent_handler)

        self.assertEqual(SyncStatus.FAILED, first.status)
        self.assertEqual("1", first.cursor_end)
        self.assertEqual(1, first.imported_count)
        self.assertEqual(1, first.failed_count)
        self.assertEqual("1", second.cursor_start)
        self.assertEqual(["1"], second_adapter.fetch_calls)
        self.assertEqual(SyncStatus.COMPLETED, second.status)
        self.assertEqual(3, self._import_count())

    def test_retryable_handler_failure_is_bounded_and_audited(self) -> None:
        attempts = 0

        def flaky_handler(
            connection: sqlite3.Connection,
            record: SyncRecord[FixturePayload],
        ) -> SyncDisposition:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise SyncError("database_busy", "fixture busy", retryable=True)
            return self._idempotent_handler(connection, record)

        report = self.runner.run(
            FakeAdapter(self.records[:1]),
            flaky_handler,
            SyncOptions(
                retry=RetryPolicy(max_attempts=3, backoff_seconds=0.5)
            ),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual([0.5, 1.0], self.sleeps)
        errors = self.connection.execute(
            """
            SELECT error_code, attempt, retryable
            FROM sync_errors
            WHERE sync_run_id = ?
            ORDER BY attempt
            """,
            (report.sync_run_id,),
        ).fetchall()
        self.assertEqual(
            [("database_busy", 1, 1), ("database_busy", 2, 1)],
            [tuple(row) for row in errors],
        )

    def test_retryable_adapter_failure_is_bounded_and_can_recover(self) -> None:
        adapter = FakeAdapter(self.records[:1])
        adapter.failures_remaining = 1

        report = self.runner.run(
            adapter,
            self._idempotent_handler,
            SyncOptions(
                retry=RetryPolicy(max_attempts=2, backoff_seconds=0.25)
            ),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual([None, None], adapter.fetch_calls)
        self.assertEqual([0.25], self.sleeps)

    def test_dry_run_rolls_back_writes_and_does_not_advance_checkpoint(self) -> None:
        report = self.runner.run(
            FakeAdapter(self.records),
            self._idempotent_handler,
            SyncOptions(dry_run=True),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertTrue(report.dry_run)
        self.assertEqual(3, report.imported_count)
        self.assertEqual(0, self._import_count())
        self.assertIsNone(
            self.connection.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source = 'fixture'"
            ).fetchone()
        )

    def test_overlapping_records_are_safe_to_process_repeatedly(self) -> None:
        first = self.runner.run(
            FakeAdapter(self.records),
            self._idempotent_handler,
        )
        second = self.runner.run(
            FakeAdapter(self.records, overlap=True),
            self._idempotent_handler,
        )

        self.assertEqual(3, first.imported_count)
        self.assertEqual(0, second.imported_count)
        self.assertEqual(3, second.skipped_count)
        self.assertEqual(3, self._import_count())

    def test_regressing_overlap_cursor_fails_without_moving_checkpoint(self) -> None:
        first = self.runner.run(
            FakeAdapter(self.records),
            self._idempotent_handler,
        )
        regressing_records = (
            SyncRecord(
                external_id="activity-overlap",
                cursor_after="2",
                payload=FixturePayload(value="overlap"),
            ),
        )

        second = self.runner.run(
            FakeAdapter(
                regressing_records,
                overlap=True,
                regress_overlap_cursor=True,
            ),
            self._idempotent_handler,
        )

        self.assertEqual(SyncStatus.COMPLETED, first.status)
        self.assertEqual(SyncStatus.FAILED, second.status)
        self.assertEqual("3", second.cursor_start)
        self.assertIsNone(second.cursor_end)
        self.assertEqual(1, second.failed_count)
        checkpoint = self.connection.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = 'fixture'"
        ).fetchone()
        error = self.connection.execute(
            """
            SELECT external_id, error_code, message
            FROM sync_errors
            WHERE sync_run_id = ?
            """,
            (second.sync_run_id,),
        ).fetchone()
        self.assertEqual("3", checkpoint["cursor"])
        self.assertEqual("activity-overlap", error["external_id"])
        self.assertEqual("invalid_adapter_response", error["error_code"])
        self.assertIn("cursor regressed", error["message"])

    def test_exhausted_adapter_retries_fail_without_advancing_checkpoint(self) -> None:
        adapter = FakeAdapter(self.records)
        adapter.failures_remaining = 2

        report = self.runner.run(
            adapter,
            self._idempotent_handler,
            SyncOptions(
                retry=RetryPolicy(max_attempts=2, backoff_seconds=0)
            ),
        )

        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual(1, report.failed_count)
        self.assertIsNone(report.cursor_end)
        self.assertEqual(0, self._import_count())

    def test_unexpected_error_message_does_not_persist_exception_details(self) -> None:
        def unsafe_handler(
            connection: sqlite3.Connection,
            record: SyncRecord[FixturePayload],
        ) -> SyncDisposition:
            raise RuntimeError("token=private-fixture-value")

        report = self.runner.run(
            FakeAdapter(self.records[:1]),
            unsafe_handler,
        )
        error = self.connection.execute(
            """
            SELECT error_code, message
            FROM sync_errors
            WHERE sync_run_id = ?
            """,
            (report.sync_run_id,),
        ).fetchone()

        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual("unexpected_error", error["error_code"])
        self.assertNotIn("private-fixture-value", error["message"])

    def _idempotent_handler(
        self,
        connection: sqlite3.Connection,
        record: SyncRecord[FixturePayload],
    ) -> SyncDisposition:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO fixture_imports (external_id, value)
            VALUES (?, ?)
            """,
            (record.external_id, record.payload.value),
        )
        if cursor.rowcount == 0:
            return SyncDisposition.SKIPPED
        return SyncDisposition.IMPORTED

    def _import_count(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM fixture_imports"
        ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
