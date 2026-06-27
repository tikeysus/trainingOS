"""Manual FIT file ingestion and normalization."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
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

MAX_ZIP_NESTING_DEPTH = 1
MAX_ZIP_MEMBERS = 1000
MAX_ZIP_MEMBER_BYTES = 500 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


@dataclass
class DiscoverySummary:
    fit_count: int = 0
    skipped_count: int = 0
    nested_zip_count: int = 0


@dataclass
class _ExtractionState:
    total_bytes: int = 0
    summary: DiscoverySummary = field(default_factory=DiscoverySummary)


@dataclass(frozen=True, slots=True)
class FitMessage:
    name: str
    fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FitFileClass:
    is_activity: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FitImportPayload:
    path: Path
    source_path: str | None = None
    skip_missing_session: bool = False


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
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._discovery_summary = DiscoverySummary()
        files: list[FitImportPayload] = []
        for source_index, path in enumerate(paths, start=1):
            resolved = path.expanduser().absolute()
            if resolved.is_dir():
                files.extend(
                    FitImportPayload(path=candidate, source_path=str(candidate))
                    for candidate in sorted(
                        candidate
                        for candidate in resolved.rglob("*")
                        if candidate.is_file() and candidate.suffix.lower() == ".fit"
                    )
                )
            elif resolved.suffix.lower() == ".zip":
                state = _ExtractionState(summary=self._discovery_summary)
                files.extend(
                    _extract_zip_fit_payloads(
                        resolved,
                        self._temporary_root(),
                        source_index,
                        state,
                    )
                )
            else:
                files.append(FitImportPayload(path=resolved, source_path=str(resolved)))
        self._payloads = tuple(_deduplicate_payloads(files))

    def fetch(
        self,
        cursor: str | None,
        limit: int,
    ) -> SyncPage[FitImportPayload]:
        previous = 0 if cursor is None else int(cursor)
        records = []
        for index, payload in enumerate(self._payloads, start=1):
            cursor_after = str(max(previous, index))
            records.append(
                SyncRecord(
                    external_id=payload.source_path or str(payload.path),
                    cursor_after=cursor_after,
                    payload=payload,
                )
            )
        return SyncPage(records=tuple(records), done=True)

    @property
    def discovery_summary(self) -> DiscoverySummary:
        return self._discovery_summary

    def cursor_is_at_or_after(self, previous: str, candidate: str) -> bool:
        return int(candidate) >= int(previous)

    def close(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def __del__(self) -> None:
        self.close()

    def _temporary_root(self) -> Path:
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory()
        return Path(self._temporary_directory.name)


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
        if not classify_fit_file(messages).is_activity:
            return SyncDisposition.SKIPPED
        try:
            parsed_for_identity = parse_fit_messages(
                messages,
                raw_record_id="raw:pending",
                source=MANUAL_FIT_SOURCE,
                fallback_external_id=_checksum_external_id(content),
                synced_at=synced_at,
                timezone=self._timezone,
            )
        except SyncError as error:
            if record.payload.skip_missing_session and error.code == "fit_session_missing":
                return SyncDisposition.SKIPPED
            raise
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


def classify_fit_file(messages: Sequence[FitMessage]) -> FitFileClass:
    file_id = _first(messages, "file_id")
    if file_id is None:
        return FitFileClass(is_activity=True)
    file_type = file_id.fields.get("type")
    if file_type is None:
        return FitFileClass(is_activity=True)
    if file_type == "activity":
        return FitFileClass(is_activity=True, reason="activity")
    return FitFileClass(is_activity=False, reason=str(file_type))


def _extract_zip_fit_payloads(
    archive_path: Path,
    target_root: Path,
    source_index: int,
    state: _ExtractionState,
) -> tuple[FitImportPayload, ...]:
    extracted: list[FitImportPayload] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            _extract_fit_members(
                archive,
                target_root,
                source_index,
                extracted,
                state,
                depth=0,
            )
    except (OSError, zipfile.BadZipFile) as error:
        raise SyncError("fit_zip_unreadable", "FIT zip archive could not be read") from error
    return tuple(extracted)


def _extract_fit_members(
    archive: zipfile.ZipFile,
    target_root: Path,
    source_index: int,
    extracted: list[FitImportPayload],
    state: _ExtractionState,
    depth: int,
) -> None:
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        raise SyncError("fit_zip_member_count_exceeded", "FIT zip archive has too many members")
    for member in sorted(members, key=lambda info: info.filename):
        if member.is_dir():
            continue
        lower_name = member.filename.lower()
        if lower_name.endswith(".fit"):
            if member.file_size > MAX_ZIP_MEMBER_BYTES:
                raise SyncError("fit_zip_member_too_large", "FIT zip member is too large")
            try:
                content = archive.read(member)
            except (OSError, zipfile.BadZipFile) as error:
                raise SyncError(
                    "fit_zip_member_unreadable",
                    "FIT zip archive member could not be read",
                ) from error
            state.total_bytes += len(content)
            if state.total_bytes > MAX_ZIP_TOTAL_BYTES:
                raise SyncError("fit_zip_total_size_exceeded", "FIT zip total uncompressed size exceeded")
            state.summary.fit_count += 1
            digest = hashlib.sha256(content).hexdigest()
            sequence = len(extracted) + 1
            extracted_path = target_root / f"fit-{sequence:06d}-{digest[:16]}.fit"
            extracted_path.write_bytes(content)
            extracted.append(
                FitImportPayload(
                    path=extracted_path,
                    source_path=(
                        f"zip-fit:{source_index}:{sequence}:sha256:{digest[:16]}"
                    ),
                    skip_missing_session=True,
                )
            )
            continue
        if lower_name.endswith(".zip"):
            if depth >= MAX_ZIP_NESTING_DEPTH:
                raise SyncError("fit_zip_depth_exceeded", "FIT zip nesting depth exceeded")
            if member.file_size > MAX_ZIP_MEMBER_BYTES:
                raise SyncError("fit_zip_member_too_large", "FIT zip member is too large")
            state.summary.nested_zip_count += 1
            try:
                content = archive.read(member)
            except (OSError, zipfile.BadZipFile) as error:
                raise SyncError(
                    "fit_zip_member_unreadable",
                    "FIT zip archive member could not be read",
                ) from error
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as nested_archive:
                    _extract_fit_members(
                        nested_archive,
                        target_root,
                        source_index,
                        extracted,
                        state,
                        depth + 1,
                    )
            except zipfile.BadZipFile as error:
                raise SyncError(
                    "fit_nested_zip_unreadable",
                    "nested FIT zip archive could not be read",
                ) from error
            continue
        state.summary.skipped_count += 1


def _deduplicate_payloads(
    payloads: Iterable[FitImportPayload],
) -> tuple[FitImportPayload, ...]:
    deduplicated: dict[str, FitImportPayload] = {}
    for payload in payloads:
        deduplicated.setdefault(payload.source_path or str(payload.path), payload)
    return tuple(deduplicated.values())


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
    updated_at = _not_before(_datetime(message, "timestamp") or started_at, started_at)
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


def _not_before(value: datetime, floor: datetime) -> datetime:
    if value < floor:
        return floor
    return value


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
