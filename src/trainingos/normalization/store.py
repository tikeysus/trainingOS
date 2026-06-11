"""Persistence of vendor-neutral activity and health records."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from trainingos.domain import (
    Activity,
    ActivitySample,
    DailyHealth,
    Lap,
    Measurement,
    MetricStatus,
    MetricValue,
    RecordMetadata,
    SourceReference,
    Unit,
)

_UNIT_TO_BASE: dict[Unit, tuple[str, float]] = {
    Unit.METRE: ("distance", 1.0),
    Unit.KILOMETRE: ("distance", 1000.0),
    Unit.SECOND: ("duration", 1.0),
    Unit.MILLISECOND: ("duration", 0.001),
    Unit.RATIO: ("ratio", 1.0),
    Unit.PERCENT: ("ratio", 0.01),
}


def convert_measurement(
    measurement: Measurement,
    target_unit: Unit,
) -> Measurement:
    """Convert between explicitly compatible units."""
    if measurement.unit is target_unit:
        return measurement
    source = _UNIT_TO_BASE.get(measurement.unit)
    target = _UNIT_TO_BASE.get(target_unit)
    if source is None or target is None or source[0] != target[0]:
        raise ValueError(
            f"cannot convert {measurement.unit.value} to {target_unit.value}"
        )
    return Measurement(
        measurement.value * source[1] / target[1],
        target_unit,
    )


class NormalizationStore:
    """Writes canonical records without committing the caller's transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert_activity(self, activity: Activity) -> None:
        if not self._upsert_record(activity.metadata, "activity"):
            return
        self._connection.execute(
            """
            INSERT INTO activities (
                record_id, activity_type, started_at, duration_seconds,
                distance_metres, title
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (record_id) DO UPDATE SET
                activity_type = excluded.activity_type,
                started_at = excluded.started_at,
                duration_seconds = excluded.duration_seconds,
                distance_metres = excluded.distance_metres,
                title = excluded.title
            """,
            (
                activity.metadata.record_id,
                activity.activity_type.value,
                _timestamp(activity.started_at),
                activity.duration.value,
                None if activity.distance is None else activity.distance.value,
                activity.title,
            ),
        )

    def upsert_lap(self, lap: Lap) -> None:
        if not self._upsert_record(lap.metadata, "lap"):
            return
        self._connection.execute(
            """
            INSERT INTO laps (
                record_id, activity_id, lap_index, started_at,
                duration_seconds, distance_metres
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (record_id) DO UPDATE SET
                activity_id = excluded.activity_id,
                lap_index = excluded.lap_index,
                started_at = excluded.started_at,
                duration_seconds = excluded.duration_seconds,
                distance_metres = excluded.distance_metres
            """,
            (
                lap.metadata.record_id,
                lap.activity_id,
                lap.index,
                _timestamp(lap.started_at),
                lap.duration.value,
                None if lap.distance is None else lap.distance.value,
            ),
        )

    def upsert_sample(self, sample: ActivitySample) -> None:
        if not self._upsert_record(sample.metadata, "activity_sample"):
            return
        self._connection.execute(
            """
            INSERT INTO activity_samples (record_id, activity_id, recorded_at)
            VALUES (?, ?, ?)
            ON CONFLICT (record_id) DO UPDATE SET
                activity_id = excluded.activity_id,
                recorded_at = excluded.recorded_at
            """,
            (
                sample.metadata.record_id,
                sample.activity_id,
                _timestamp(sample.recorded_at),
            ),
        )
        self._upsert_metrics(
            sample.metadata.record_id,
            sample.metrics,
        )

    def upsert_daily_health(self, health: DailyHealth) -> None:
        if not self._upsert_record(health.metadata, "daily_health"):
            return
        self._connection.execute(
            """
            INSERT INTO daily_health (record_id, local_date)
            VALUES (?, ?)
            ON CONFLICT (record_id) DO UPDATE SET
                local_date = excluded.local_date
            """,
            (health.metadata.record_id, health.local_date.isoformat()),
        )
        self._upsert_metrics(
            health.metadata.record_id,
            health.metrics,
        )

    def _upsert_record(self, metadata: RecordMetadata, record_type: str) -> bool:
        self._validate_source_references(metadata.source_references)
        existing = self._connection.execute(
            """
            SELECT record_type, updated_at
            FROM records
            WHERE record_id = ?
            """,
            (metadata.record_id,),
        ).fetchone()
        incoming_updated_at = _timestamp(metadata.updated_at)
        if existing is not None:
            if existing["record_type"] != record_type:
                raise ValueError(
                    f"record {metadata.record_id} already has type "
                    f"{existing['record_type']}"
                )
            if existing["updated_at"] > incoming_updated_at:
                self._upsert_source_references(
                    metadata.record_id,
                    metadata.source_references,
                )
                return False

        provenance = metadata.provenance
        self._connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind, method_name, method_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (record_id) DO UPDATE SET
                timezone = excluded.timezone,
                updated_at = excluded.updated_at,
                provenance_kind = excluded.provenance_kind,
                method_name = excluded.method_name,
                method_version = excluded.method_version
            """,
            (
                metadata.record_id,
                record_type,
                metadata.timezone,
                _timestamp(metadata.created_at),
                incoming_updated_at,
                None if provenance is None else provenance.kind.value,
                None
                if provenance is None or provenance.method is None
                else provenance.method.name,
                None
                if provenance is None or provenance.method is None
                else provenance.method.version,
            ),
        )
        self._upsert_source_references(
            metadata.record_id,
            metadata.source_references,
        )
        self._replace_provenance(metadata)
        return True

    def _validate_source_references(
        self,
        references: tuple[SourceReference, ...],
    ) -> None:
        if not references:
            raise ValueError("normalized record requires a source reference")
        for reference in references:
            if reference.raw_reference is None:
                raise ValueError(
                    "normalized source reference requires a raw reference"
                )
            raw = self._connection.execute(
                """
                SELECT source, external_id
                FROM raw_source_records
                WHERE raw_record_id = ?
                """,
                (reference.raw_reference,),
            ).fetchone()
            if raw is None:
                raise ValueError(
                    f"raw source record does not exist: {reference.raw_reference}"
                )
            if raw["source"] != reference.source:
                raise ValueError(
                    "source reference source does not match its raw source record"
                )

    def _upsert_source_references(
        self,
        record_id: str,
        references: tuple[SourceReference, ...],
    ) -> None:
        for reference in references:
            parser = reference.parser
            self._connection.execute(
                """
                INSERT INTO source_references (
                    record_id, source, external_id, synced_at, raw_record_id,
                    parser_name, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (record_id, source, external_id) DO UPDATE SET
                    synced_at = CASE
                        WHEN excluded.synced_at > source_references.synced_at
                        THEN excluded.synced_at
                        ELSE source_references.synced_at
                    END,
                    raw_record_id = CASE
                        WHEN excluded.synced_at >= source_references.synced_at
                        THEN excluded.raw_record_id
                        ELSE source_references.raw_record_id
                    END,
                    parser_name = CASE
                        WHEN excluded.synced_at >= source_references.synced_at
                        THEN excluded.parser_name
                        ELSE source_references.parser_name
                    END,
                    parser_version = CASE
                        WHEN excluded.synced_at >= source_references.synced_at
                        THEN excluded.parser_version
                        ELSE source_references.parser_version
                    END
                """,
                (
                    record_id,
                    reference.source,
                    reference.external_id,
                    _timestamp(reference.synced_at),
                    reference.raw_reference,
                    None if parser is None else parser.name,
                    None if parser is None else parser.version,
                ),
            )

    def _replace_provenance(self, metadata: RecordMetadata) -> None:
        self._connection.execute(
            "DELETE FROM provenance_evidence WHERE record_id = ?",
            (metadata.record_id,),
        )
        self._connection.execute(
            "DELETE FROM provenance_caveats WHERE record_id = ?",
            (metadata.record_id,),
        )
        if metadata.provenance is None:
            return
        self._connection.executemany(
            """
            INSERT INTO provenance_evidence (
                record_id, evidence_record_id, position
            ) VALUES (?, ?, ?)
            """,
            (
                (metadata.record_id, evidence_id, position)
                for position, evidence_id in enumerate(
                    metadata.provenance.evidence_record_ids
                )
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO provenance_caveats (record_id, position, caveat)
            VALUES (?, ?, ?)
            """,
            (
                (metadata.record_id, position, caveat)
                for position, caveat in enumerate(metadata.provenance.caveats)
            ),
        )

    def _upsert_metrics(
        self,
        owner_record_id: str,
        metrics: tuple[MetricValue, ...],
    ) -> None:
        seen: set[str] = set()
        for metric in metrics:
            if metric.key in seen:
                raise ValueError(f"duplicate metric key: {metric.key}")
            seen.add(metric.key)
            existing = self._connection.execute(
                """
                SELECT metric_status, quality
                FROM metric_values
                WHERE owner_record_id = ? AND metric_key = ?
                """,
                (owner_record_id, metric.key),
            ).fetchone()
            if existing is not None and not _can_replace_metric(existing, metric):
                continue
            measurement = metric.measurement
            self._connection.execute(
                """
                INSERT INTO metric_values (
                    owner_record_id, metric_key, metric_status, metric_value,
                    unit, quality, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (owner_record_id, metric_key) DO UPDATE SET
                    metric_status = excluded.metric_status,
                    metric_value = excluded.metric_value,
                    unit = excluded.unit,
                    quality = excluded.quality,
                    attributes_json = excluded.attributes_json
                """,
                (
                    owner_record_id,
                    metric.key,
                    metric.status.value,
                    None if measurement is None else measurement.value,
                    None if measurement is None else measurement.unit.value,
                    metric.quality,
                    json.dumps(dict(metric.attributes), sort_keys=True),
                ),
            )


def _can_replace_metric(
    existing: sqlite3.Row,
    incoming: MetricValue,
) -> bool:
    existing_measured = existing["metric_status"] == MetricStatus.MEASURED.value
    incoming_measured = incoming.status is MetricStatus.MEASURED
    if existing_measured and not incoming_measured:
        return False
    if not existing_measured:
        return True
    if not incoming_measured:
        return False
    existing_quality = existing["quality"]
    incoming_quality = incoming.quality
    return (0.0 if incoming_quality is None else incoming_quality) >= (
        0.0 if existing_quality is None else existing_quality
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
