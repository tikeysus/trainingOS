"""Manual FIT file ingestion and normalization."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trainingos.domain import (
    Activity,
    ActivitySample,
    ActivityType,
    Lap,
    Measurement,
    MethodVersion,
    MetricValue,
    RecordMetadata,
    SourceReference,
    Unit,
)
from trainingos.ingestion.raw import RawArtifactStore
from trainingos.ingestion.sync import SyncDisposition, SyncError, SyncPage, SyncRecord
from trainingos.normalization import NormalizationStore

MANUAL_FIT_SOURCE = "manual_fit"
FIT_CONTENT_TYPE = "application/vnd.ant.fit"
FIT_PARSER = MethodVersion("fitdecode_fit_parser", "1.0.0")


@dataclass(frozen=True, slots=True)
class FitMessage:
    name: str
    fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FitImportPayload:
    path: Path


@dataclass(frozen=True, slots=True)
class ParsedFitActivity:
    external_id: str
    source_updated_at: datetime | None
    activity: Activity
    laps: tuple[Lap, ...]
    samples: tuple[ActivitySample, ...]


class ManualFitAdapter:
    source = MANUAL_FIT_SOURCE

    def __init__(self, paths: Iterable[Path]) -> None:
        files = []
        for path in paths:
            resolved = path.expanduser().absolute()
            if resolved.is_dir():
                files.extend(
                    sorted(
                        candidate
                        for candidate in resolved.rglob("*")
                        if candidate.is_file()
                        and candidate.suffix.lower() == ".fit"
                    )
                )
            else:
                files.append(resolved)
        self._paths = tuple(dict.fromkeys(files))

    def fetch(
        self,
        cursor: str | None,
        limit: int,
    ) -> SyncPage[FitImportPayload]:
        previous = 0 if cursor is None else int(cursor)
        records = []
        for index, path in enumerate(self._paths, start=1):
            cursor_after = str(max(previous, index))
            records.append(
                SyncRecord(
                    external_id=str(path),
                    cursor_after=cursor_after,
                    payload=FitImportPayload(path=path),
                )
            )
        return SyncPage(records=tuple(records), done=True)

    def cursor_is_at_or_after(self, previous: str, candidate: str) -> bool:
        return int(candidate) >= int(previous)


class ManualFitHandler:
    def __init__(
        self,
        raw_store: RawArtifactStore,
        *,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._raw_store = raw_store
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(
        self,
        connection: sqlite3.Connection,
        record: SyncRecord[FitImportPayload],
    ) -> SyncDisposition:
        path = record.payload.path
        try:
            content = path.read_bytes()
        except OSError as error:
            raise SyncError("fit_file_unreadable", "FIT file could not be read") from error

        synced_at = _ensure_aware(self._clock(), "clock")
        messages = read_fit_messages(path)
        parsed_for_identity = parse_fit_messages(
            messages,
            raw_record_id="raw:pending",
            source=MANUAL_FIT_SOURCE,
            fallback_external_id=_checksum_external_id(content),
            synced_at=synced_at,
            timezone=self._timezone,
        )
        sync_run_id = _current_sync_run_id(connection, MANUAL_FIT_SOURCE)
        raw = self._raw_store.retain_bytes(
            connection,
            sync_run_id=sync_run_id,
            source=MANUAL_FIT_SOURCE,
            external_id=parsed_for_identity.external_id,
            record_kind="activity_file",
            content_type=FIT_CONTENT_TYPE,
            content=content,
            source_updated_at=parsed_for_identity.source_updated_at,
            ingested_at=synced_at,
        )
        parsed = parse_fit_messages(
            messages,
            raw_record_id=raw.raw_record_id,
            source=MANUAL_FIT_SOURCE,
            fallback_external_id=parsed_for_identity.external_id,
            synced_at=synced_at,
            timezone=self._timezone,
        )
        store = NormalizationStore(connection)
        existing = connection.execute(
            """
            SELECT record_id
            FROM source_references
            WHERE source = ? AND external_id = ?
            """,
            (MANUAL_FIT_SOURCE, parsed.external_id),
        ).fetchone()
        store.upsert_activity(parsed.activity)
        for lap in parsed.laps:
            store.upsert_lap(lap)
        for sample in parsed.samples:
            store.upsert_sample(sample)
        return SyncDisposition.SKIPPED if existing is not None else SyncDisposition.IMPORTED


def read_fit_messages(path: Path) -> tuple[FitMessage, ...]:
    try:
        import fitdecode
    except ImportError as error:
        raise SyncError(
            "fitdecode_missing",
            "fitdecode is required to parse FIT files",
        ) from error

    messages: list[FitMessage] = []
    try:
        with fitdecode.FitReader(str(path)) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.records.FitDataMessage):
                    continue
                fields = {
                    field.name: field.value
                    for field in frame.fields
                    if field.value is not None
                }
                messages.append(FitMessage(frame.name, fields))
    except Exception as error:
        raise SyncError("fit_parse_failed", "FIT file could not be parsed") from error
    return tuple(messages)


def parse_fit_messages(
    messages: Sequence[FitMessage],
    *,
    raw_record_id: str,
    source: str,
    fallback_external_id: str,
    synced_at: datetime,
    timezone: str,
) -> ParsedFitActivity:
    synced_at = _ensure_aware(synced_at, "synced_at")
    file_id = _first(messages, "file_id")
    session = _first(messages, "session")
    if session is None:
        raise SyncError("fit_session_missing", "FIT activity session is missing")

    start_time = _datetime(session, "start_time") or _datetime(session, "timestamp")
    if start_time is None:
        raise SyncError("fit_start_missing", "FIT activity start time is missing")
    duration = _number(session, "total_timer_time", "total_elapsed_time")
    if duration is None:
        raise SyncError("fit_duration_missing", "FIT activity duration is missing")

    external_id = _external_id(file_id, session, fallback_external_id)
    source_updated_at = _datetime(session, "timestamp") or start_time
    activity_id = _record_id(source, external_id, "activity")
    reference = _reference(
        source,
        external_id,
        synced_at,
        raw_record_id,
    )
    activity = Activity(
        metadata=RecordMetadata(
            record_id=activity_id,
            timezone=timezone,
            created_at=start_time,
            updated_at=source_updated_at,
            source_references=(reference,),
        ),
        activity_type=_activity_type(session.fields.get("sport")),
        started_at=start_time,
        duration=Measurement(duration, Unit.SECOND),
        distance=_measurement(session, Unit.METRE, "total_distance"),
        title=_title(session),
    )
    laps = tuple(
        _lap(
            message,
            index,
            activity_id,
            source,
            external_id,
            synced_at,
            raw_record_id,
            timezone,
        )
        for index, message in enumerate(_messages(messages, "lap"))
    )
    samples = tuple(
        sample
        for sample in (
            _sample(
                message,
                index,
                activity_id,
                source,
                external_id,
                synced_at,
                raw_record_id,
                timezone,
            )
            for index, message in enumerate(_messages(messages, "record"))
        )
        if sample is not None
    )
    return ParsedFitActivity(
        external_id=external_id,
        source_updated_at=source_updated_at,
        activity=activity,
        laps=laps,
        samples=samples,
    )


def _lap(
    message: FitMessage,
    index: int,
    activity_id: str,
    source: str,
    external_id: str,
    synced_at: datetime,
    raw_record_id: str,
    timezone: str,
) -> Lap:
    started_at = _datetime(message, "start_time") or _datetime(message, "timestamp")
    duration = _number(message, "total_timer_time", "total_elapsed_time")
    if started_at is None or duration is None:
        raise SyncError("fit_lap_invalid", "FIT lap is missing start or duration")
    updated_at = _datetime(message, "timestamp") or started_at
    lap_external_id = f"{external_id}:lap:{index}"
    return Lap(
        metadata=RecordMetadata(
            record_id=_record_id(source, lap_external_id, "lap"),
            timezone=timezone,
            created_at=started_at,
            updated_at=updated_at,
            source_references=(
                _reference(source, lap_external_id, synced_at, raw_record_id),
            ),
        ),
        activity_id=activity_id,
        index=index,
        started_at=started_at,
        duration=Measurement(duration, Unit.SECOND),
        distance=_measurement(message, Unit.METRE, "total_distance"),
    )


def _sample(
    message: FitMessage,
    index: int,
    activity_id: str,
    source: str,
    external_id: str,
    synced_at: datetime,
    raw_record_id: str,
    timezone: str,
) -> ActivitySample | None:
    recorded_at = _datetime(message, "timestamp")
    if recorded_at is None:
        return None
    metrics = _sample_metrics(message)
    if not metrics:
        return None
    sample_external_id = f"{external_id}:sample:{index}"
    return ActivitySample(
        metadata=RecordMetadata(
            record_id=_record_id(source, sample_external_id, "sample"),
            timezone=timezone,
            created_at=recorded_at,
            updated_at=recorded_at,
            source_references=(
                _reference(source, sample_external_id, synced_at, raw_record_id),
            ),
        ),
        activity_id=activity_id,
        recorded_at=recorded_at,
        metrics=tuple(metrics),
    )


def _sample_metrics(message: FitMessage) -> list[MetricValue]:
    metric_specs = (
        ("heart_rate", Unit.BEAT_PER_MINUTE, ("heart_rate",)),
        ("speed", Unit.METRE_PER_SECOND, ("enhanced_speed", "speed")),
        ("distance", Unit.METRE, ("distance",)),
        ("cadence", Unit.COUNT, ("cadence",)),
        ("power", Unit.WATT, ("power",)),
        ("altitude", Unit.METRE, ("enhanced_altitude", "altitude")),
        ("temperature", Unit.DEGREE_CELSIUS, ("temperature",)),
    )
    metrics: list[MetricValue] = []
    for key, unit, field_names in metric_specs:
        value = _number(message, *field_names)
        if value is None:
            continue
        metrics.append(MetricValue(key, Measurement(value, unit), quality=1.0))
    return metrics


def _reference(
    source: str,
    external_id: str,
    synced_at: datetime,
    raw_record_id: str,
) -> SourceReference:
    return SourceReference(
        source=source,
        external_id=external_id,
        synced_at=synced_at,
        raw_reference=raw_record_id,
        parser=FIT_PARSER,
    )


def _external_id(
    file_id: FitMessage | None,
    session: FitMessage,
    fallback: str,
) -> str:
    if file_id is None:
        return fallback
    serial = file_id.fields.get("serial_number")
    created_at = _datetime(file_id, "time_created") or _datetime(session, "start_time")
    if serial is None or created_at is None:
        return fallback
    return f"fit:{serial}:{created_at.astimezone(UTC).isoformat()}"


def _checksum_external_id(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _record_id(source: str, external_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"{source}:{external_id}:{kind}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _current_sync_run_id(connection: sqlite3.Connection, source: str) -> str:
    row = connection.execute(
        """
        SELECT sync_run_id
        FROM sync_runs
        WHERE source = ? AND status = 'running'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
    if row is None:
        raise SyncError("sync_run_missing", "running sync run is missing")
    return row["sync_run_id"]


def _first(messages: Sequence[FitMessage], name: str) -> FitMessage | None:
    return next((message for message in messages if message.name == name), None)


def _messages(messages: Sequence[FitMessage], name: str) -> tuple[FitMessage, ...]:
    return tuple(message for message in messages if message.name == name)


def _number(message: FitMessage, *field_names: str) -> float | None:
    for field_name in field_names:
        value = message.fields.get(field_name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float):
            return float(value)
    return None


def _measurement(
    message: FitMessage,
    unit: Unit,
    *field_names: str,
) -> Measurement | None:
    value = _number(message, *field_names)
    return None if value is None else Measurement(value, unit)


def _datetime(message: FitMessage, field_name: str) -> datetime | None:
    value = message.fields.get(field_name)
    if not isinstance(value, datetime):
        return None
    return _ensure_aware(value, field_name)


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _activity_type(sport: object) -> ActivityType:
    if sport == "running":
        return ActivityType.RUN
    if sport == "walking":
        return ActivityType.WALK
    if sport in {"cycling", "biking"}:
        return ActivityType.RIDE
    return ActivityType.OTHER


def _title(session: FitMessage) -> str | None:
    sport = session.fields.get("sport")
    if isinstance(sport, str) and sport:
        return sport.replace("_", " ").title()
    return None
