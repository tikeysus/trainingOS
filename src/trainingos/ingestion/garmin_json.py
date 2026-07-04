"""Garmin account-export JSON health importer (sleep and daily summary)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.domain import (
    DailyHealth,
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

GARMIN_JSON_SOURCE = "garmin_json"
GARMIN_JSON_PARSER = MethodVersion("garmin_json_health_parser", "1.0.0")
_JSON_CONTENT_TYPE = "application/json"

_WELLNESS_PREFIXES = ("wellnessSleepData", "wellnessEpochSummaries")

_PII_PATHS = frozenset(
    [
        "customer_data/customer.json",
        "DI_CONNECT/DI-Connect-User/user_profile.json",
        "DI_CONNECT/DI-Connect-User/user_biometrics.json",
    ]
)


@dataclass(frozen=True, slots=True)
class GarminJsonPayload:
    content: bytes
    file_type: str  # "sleep" | "summary"
    source_path: str


# ---------------------------------------------------------------------------
# Pure parsers (no DB; raw_record_id is supplied by the caller)
# ---------------------------------------------------------------------------


def parse_garmin_sleep_json(
    records: list[dict],
    *,
    raw_record_id: str,
    synced_at: datetime,
    timezone: str,
) -> list[DailyHealth]:
    results: list[DailyHealth] = []
    for record in records:
        dto = record["dailySleepDTO"]
        local_date = _parse_date(dto, "calendarDate")
        metrics: list[MetricValue] = []
        _add_metric(metrics, "sleep_duration", dto.get("sleepTimeSeconds"), Unit.SECOND)
        _add_metric(metrics, "sleep_deep", dto.get("deepSleepSeconds"), Unit.SECOND)
        _add_metric(metrics, "sleep_light", dto.get("lightSleepSeconds"), Unit.SECOND)
        _add_metric(metrics, "sleep_rem", dto.get("remSleepSeconds"), Unit.SECOND)
        _add_metric(metrics, "sleep_awake", dto.get("awakeSleepSeconds"), Unit.SECOND)
        _add_metric(metrics, "hrv_overnight", dto.get("averageHRV"), Unit.MILLISECOND)
        _add_metric(metrics, "spo2_average", dto.get("averageSpO2Value"), Unit.PERCENT)
        results.append(_daily_health(local_date, timezone, synced_at, raw_record_id, metrics))
    results.sort(key=lambda h: h.local_date)
    return results


def parse_garmin_daily_summary_json(
    records: list[dict],
    *,
    raw_record_id: str,
    synced_at: datetime,
    timezone: str,
) -> list[DailyHealth]:
    results: list[DailyHealth] = []
    for record in records:
        local_date = _parse_date(record, "calendarDate")
        metrics: list[MetricValue] = []
        _add_metric(metrics, "resting_heart_rate", record.get("restingHeartRate"), Unit.BEAT_PER_MINUTE)
        _add_metric(metrics, "stress_average", record.get("averageStressLevel"), Unit.COUNT)
        _add_metric(metrics, "stress_max", record.get("maxStressLevel"), Unit.COUNT)
        _add_metric(metrics, "body_battery_high", record.get("bodyBatteryHighestValue"), Unit.COUNT)
        _add_metric(metrics, "body_battery_low", record.get("bodyBatteryLowestValue"), Unit.COUNT)
        _add_metric(
            metrics, "vo2_max", record.get("vo2Max"), Unit.MILLILITRE_PER_KILOGRAM_PER_MINUTE
        )
        results.append(_daily_health(local_date, timezone, synced_at, raw_record_id, metrics))
    results.sort(key=lambda h: h.local_date)
    return results


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GarminJsonHealthAdapter:
    source = GARMIN_JSON_SOURCE

    def __init__(self, paths: Iterable[Path]) -> None:
        payloads: list[GarminJsonPayload] = []
        for path in paths:
            resolved = path.expanduser().absolute()
            if resolved.is_dir():
                _collect_from_directory(resolved, payloads)
            else:
                _collect_from_zip(resolved, payloads)
        self._payloads = tuple(payloads)

    def fetch(self, cursor: str | None, limit: int) -> SyncPage[GarminJsonPayload]:
        records = tuple(
            SyncRecord(
                external_id=_checksum_external_id(payload.content),
                cursor_after=str(index),
                payload=payload,
            )
            for index, payload in enumerate(self._payloads, start=1)
        )
        return SyncPage(records=records, done=True)

    def cursor_is_at_or_after(self, previous: str, candidate: str) -> bool:
        return int(candidate) >= int(previous)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class GarminJsonHealthHandler:
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
        record: SyncRecord[GarminJsonPayload],
    ) -> SyncDisposition:
        existing = connection.execute(
            "SELECT raw_record_id FROM raw_source_records WHERE source = ? AND external_id = ?",
            (GARMIN_JSON_SOURCE, record.external_id),
        ).fetchone()
        if existing is not None:
            return SyncDisposition.SKIPPED

        synced_at = _ensure_utc(self._clock())
        sync_run_id = _current_sync_run_id(connection, GARMIN_JSON_SOURCE)
        raw = self._raw_store.retain_bytes(
            connection,
            sync_run_id=sync_run_id,
            source=GARMIN_JSON_SOURCE,
            external_id=record.external_id,
            record_kind="health_json",
            content_type=_JSON_CONTENT_TYPE,
            content=record.payload.content,
            source_updated_at=None,
            ingested_at=synced_at,
        )

        try:
            data = json.loads(record.payload.content)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SyncError("garmin_json_parse_failed", "Garmin JSON could not be parsed") from exc

        if record.payload.file_type == "sleep":
            parsed = parse_garmin_sleep_json(
                data,
                raw_record_id=raw.raw_record_id,
                synced_at=synced_at,
                timezone=self._timezone,
            )
        else:
            parsed = parse_garmin_daily_summary_json(
                data,
                raw_record_id=raw.raw_record_id,
                synced_at=synced_at,
                timezone=self._timezone,
            )

        store = NormalizationStore(connection)
        for health in parsed:
            store.upsert_daily_health(health)

        return SyncDisposition.IMPORTED


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_date(mapping: dict, field: str) -> date:
    raw = mapping.get(field)
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"calendarDate is not a valid ISO date: {raw!r}") from exc


def _add_metric(metrics: list[MetricValue], key: str, value: object, unit: Unit) -> None:
    if value is None:
        return
    metrics.append(MetricValue(key, Measurement(float(value), unit), quality=1.0))


def _daily_health(
    local_date: date,
    timezone: str,
    synced_at: datetime,
    raw_record_id: str,
    metrics: list[MetricValue],
) -> DailyHealth:
    record_id = _record_id(GARMIN_JSON_SOURCE, local_date.isoformat(), "daily_health")
    ref = SourceReference(
        source=GARMIN_JSON_SOURCE,
        external_id=local_date.isoformat(),
        synced_at=synced_at,
        raw_reference=raw_record_id,
        parser=GARMIN_JSON_PARSER,
    )
    metadata = RecordMetadata(
        record_id=record_id,
        timezone=timezone,
        created_at=synced_at,
        updated_at=synced_at,
        source_references=(ref,),
    )
    return DailyHealth(metadata=metadata, local_date=local_date, metrics=tuple(metrics))


def _record_id(source: str, external_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"{source}:{external_id}:{kind}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _checksum_external_id(content: bytes) -> str:
    return f"garmin-json:sha256:{hashlib.sha256(content).hexdigest()}"


def _current_sync_run_id(connection: sqlite3.Connection, source: str) -> str:
    row = connection.execute(
        """
        SELECT sync_run_id FROM sync_runs
        WHERE source = ? AND status = 'running'
        ORDER BY started_at DESC LIMIT 1
        """,
        (source,),
    ).fetchone()
    if row is None:
        raise SyncError("sync_run_missing", "running sync run is missing")
    return row["sync_run_id"]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_pii(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized in _PII_PATHS:
        return True
    filename = normalized.rsplit("/", 1)[-1]
    return "@" in filename


def _file_type(name: str) -> str | None:
    filename = name.rsplit("/", 1)[-1]
    if filename.startswith("wellnessSleepData"):
        return "sleep"
    if filename.startswith("wellnessEpochSummaries"):
        return "summary"
    return None


def _collect_from_directory(directory: Path, payloads: list[GarminJsonPayload]) -> None:
    for path in sorted(directory.rglob("*.json")):
        if _is_pii(path.name):
            continue
        file_type = _file_type(path.name)
        if file_type is None:
            continue
        payloads.append(
            GarminJsonPayload(
                content=path.read_bytes(),
                file_type=file_type,
                source_path=str(path),
            )
        )


def _collect_from_zip(archive_path: Path, payloads: list[GarminJsonPayload]) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                name = info.filename
                if _is_pii(name):
                    continue
                file_type = _file_type(name)
                if file_type is None:
                    continue
                content = archive.read(info)
                payloads.append(
                    GarminJsonPayload(
                        content=content,
                        file_type=file_type,
                        source_path=f"zip:{archive_path}:{name}",
                    )
                )
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid ZIP archive: {archive_path}") from exc
