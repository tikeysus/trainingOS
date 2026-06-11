from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trainingos.domain import (
    Activity,
    ActivitySample,
    ActivityType,
    DailyHealth,
    Lap,
    Measurement,
    MethodVersion,
    MetricStatus,
    MetricValue,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    SourceReference,
    Unit,
)
from trainingos.normalization import NormalizationStore, convert_measurement
from trainingos.storage import apply_migrations, connect_database


class NormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        self.connection = connect_database(database_path)
        apply_migrations(self.connection)
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-1', 'fixture', 'completed',
                '2026-11-01T06:00:00+00:00',
                '2026-11-01T06:01:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, payload, ingested_at
            ) VALUES (
                'raw-activity-1', 'run-1', 'fixture', 'source-file-1',
                'activity_file', 'application/octet-stream', 'sha256:fixture',
                X'00', '2026-11-01T06:00:00+00:00'
            )
            """
        )
        self.connection.commit()
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_persists_activity_lap_sample_and_daily_health_with_lineage(
        self,
    ) -> None:
        activity = self._activity()
        lap = Lap(
            metadata=self._metadata("lap-1", "activity-1:lap:0"),
            activity_id=activity.metadata.record_id,
            index=0,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(300, Unit.SECOND),
            distance=Measurement(1000, Unit.METRE),
        )
        sample = ActivitySample(
            metadata=self._metadata("sample-1", "activity-1:sample:0"),
            activity_id=activity.metadata.record_id,
            recorded_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(
                MetricValue(
                    "heart_rate",
                    Measurement(150, Unit.BEAT_PER_MINUTE),
                    quality=0.9,
                ),
            ),
        )
        health = DailyHealth(
            metadata=self._metadata("health-1", "health:2026-11-01"),
            local_date=date(2026, 11, 1),
            metrics=(
                MetricValue("sleep_duration", Measurement(27000, Unit.SECOND)),
                MetricValue("hrv", Measurement(52, Unit.MILLISECOND)),
                MetricValue(
                    "resting_heart_rate",
                    Measurement(48, Unit.BEAT_PER_MINUTE),
                ),
                MetricValue("stress", Measurement(0, Unit.COUNT)),
                MetricValue("body_battery", Measurement(75, Unit.COUNT)),
                MetricValue(
                    "spo2",
                    None,
                    status=MetricStatus.UNAVAILABLE,
                ),
                MetricValue(
                    "vo2_max",
                    Measurement(
                        55,
                        Unit.MILLILITRE_PER_KILOGRAM_PER_MINUTE,
                    ),
                ),
                MetricValue(
                    "respiration_rate",
                    None,
                    status=MetricStatus.UNSUPPORTED,
                ),
            ),
        )

        self.store.upsert_activity(activity)
        self.store.upsert_lap(lap)
        self.store.upsert_sample(sample)
        self.store.upsert_daily_health(health)

        self.assertEqual(
            4,
            self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
        )
        self.assertEqual(
            4,
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM source_references AS reference
                JOIN raw_source_records AS raw
                  ON raw.raw_record_id = reference.raw_record_id
                JOIN sync_runs AS run
                  ON run.sync_run_id = raw.sync_run_id
                """
            ).fetchone()[0],
        )
        reference = self.connection.execute(
            """
            SELECT parser_name, parser_version
            FROM source_references
            WHERE record_id = 'activity-1'
            """
        ).fetchone()
        self.assertEqual(("fixture_parser", "1.0.0"), tuple(reference))
        statuses = {
            row["metric_key"]: (row["metric_status"], row["metric_value"])
            for row in self.connection.execute(
                """
                SELECT metric_key, metric_status, metric_value
                FROM metric_values
                WHERE owner_record_id = 'health-1'
                """
            )
        }
        self.assertEqual(("measured", 27000.0), statuses["sleep_duration"])
        self.assertEqual(("unavailable", None), statuses["spo2"])
        self.assertEqual(("measured", 0.0), statuses["stress"])
        self.assertEqual(("measured", 75.0), statuses["body_battery"])
        self.assertEqual(("measured", 55.0), statuses["vo2_max"])
        self.assertEqual(("unsupported", None), statuses["respiration_rate"])

    def test_reimport_is_idempotent_and_newer_core_values_replace_older(self) -> None:
        activity = self._activity(title="First title")
        self.store.upsert_activity(activity)
        self.store.upsert_activity(activity)

        newer = Activity(
            metadata=self._metadata(
                "activity-1",
                "activity-1",
                updated_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
            ),
            activity_type=ActivityType.RUN,
            started_at=activity.started_at,
            duration=activity.duration,
            distance=activity.distance,
            title="Corrected title",
        )
        self.store.upsert_activity(newer)

        self.assertEqual(
            1,
            self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
        )
        self.assertEqual(
            1,
            self.connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
        )
        self.assertEqual(
            "Corrected title",
            self.connection.execute(
                "SELECT title FROM activities WHERE record_id = 'activity-1'"
            ).fetchone()["title"],
        )

    def test_stale_core_record_and_lower_quality_metric_do_not_overwrite(self) -> None:
        activity = self._activity(title="Current")
        self.store.upsert_activity(activity)
        stale = Activity(
            metadata=self._metadata(
                "activity-1",
                "activity-1",
                updated_at=datetime(2026, 10, 31, 23, 0, tzinfo=UTC),
            ),
            activity_type=ActivityType.RUN,
            started_at=activity.started_at,
            duration=activity.duration,
            distance=activity.distance,
            title="Stale",
        )
        self.store.upsert_activity(stale)

        sample = ActivitySample(
            metadata=self._metadata("sample-1", "activity-1:sample:0"),
            activity_id="activity-1",
            recorded_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(
                MetricValue(
                    "heart_rate",
                    Measurement(150, Unit.BEAT_PER_MINUTE),
                    quality=0.9,
                ),
            ),
        )
        self.store.upsert_sample(sample)
        lower_quality = ActivitySample(
            metadata=self._metadata(
                "sample-1",
                "activity-1:sample:0",
                updated_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
            ),
            activity_id="activity-1",
            recorded_at=sample.recorded_at,
            metrics=(
                MetricValue(
                    "heart_rate",
                    Measurement(120, Unit.BEAT_PER_MINUTE),
                    quality=0.4,
                ),
            ),
        )
        self.store.upsert_sample(lower_quality)

        self.assertEqual(
            "Current",
            self.connection.execute(
                "SELECT title FROM activities WHERE record_id = 'activity-1'"
            ).fetchone()["title"],
        )
        metric = self.connection.execute(
            """
            SELECT metric_value, quality
            FROM metric_values
            WHERE owner_record_id = 'sample-1' AND metric_key = 'heart_rate'
            """
        ).fetchone()
        self.assertEqual((150.0, 0.9), tuple(metric))

    def test_source_identity_cannot_map_to_two_local_records(self) -> None:
        self.store.upsert_activity(self._activity())
        duplicate = Activity(
            metadata=self._metadata("activity-2", "activity-1"),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert_activity(duplicate)

    def test_normalized_record_requires_matching_retained_raw_source(self) -> None:
        missing_raw = Activity(
            metadata=RecordMetadata(
                record_id="activity-missing",
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id="activity-missing",
                        synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                        raw_reference="raw-missing",
                    ),
                ),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
        )

        with self.assertRaisesRegex(ValueError, "raw source record does not exist"):
            self.store.upsert_activity(missing_raw)

    def test_dst_fallback_instants_remain_distinct_in_utc(self) -> None:
        toronto = ZoneInfo("America/Toronto")
        activity = self._activity()
        self.store.upsert_activity(activity)
        first = ActivitySample(
            metadata=self._metadata("sample-first", "sample-first"),
            activity_id="activity-1",
            recorded_at=datetime(2026, 11, 1, 1, 30, tzinfo=toronto, fold=0),
            metrics=(MetricValue("heart_rate", Measurement(140, Unit.BEAT_PER_MINUTE)),),
        )
        second = ActivitySample(
            metadata=self._metadata("sample-second", "sample-second"),
            activity_id="activity-1",
            recorded_at=datetime(2026, 11, 1, 1, 30, tzinfo=toronto, fold=1),
            metrics=(MetricValue("heart_rate", Measurement(145, Unit.BEAT_PER_MINUTE)),),
        )

        self.store.upsert_sample(first)
        self.store.upsert_sample(second)

        instants = [
            row["recorded_at"]
            for row in self.connection.execute(
                "SELECT recorded_at FROM activity_samples ORDER BY recorded_at"
            )
        ]
        self.assertEqual(
            [
                "2026-11-01T05:30:00+00:00",
                "2026-11-01T06:30:00+00:00",
            ],
            instants,
        )

    def test_explicit_unit_conversion_rejects_incompatible_dimensions(self) -> None:
        self.assertEqual(
            Measurement(5000, Unit.METRE),
            convert_measurement(Measurement(5, Unit.KILOMETRE), Unit.METRE),
        )
        converted_ratio = convert_measurement(
            Measurement(95, Unit.PERCENT),
            Unit.RATIO,
        )
        self.assertEqual(Unit.RATIO, converted_ratio.unit)
        self.assertAlmostEqual(0.95, converted_ratio.value)
        with self.assertRaisesRegex(ValueError, "cannot convert"):
            convert_measurement(
                Measurement(5, Unit.KILOMETRE),
                Unit.SECOND,
            )

    def _activity(self, *, title: str = "Morning run") -> Activity:
        return Activity(
            metadata=self._metadata("activity-1", "activity-1"),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
            distance=Measurement(10000, Unit.METRE),
            title=title,
        )

    def _metadata(
        self,
        record_id: str,
        external_id: str,
        *,
        updated_at: datetime | None = None,
    ) -> RecordMetadata:
        timestamp = updated_at or datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
        return RecordMetadata(
            record_id=record_id,
            timezone="America/Toronto",
            created_at=min(timestamp, datetime(2026, 11, 1, 7, 0, tzinfo=UTC)),
            updated_at=timestamp,
            source_references=(
                SourceReference(
                    source="fixture",
                    external_id=external_id,
                    synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                    raw_reference="raw-activity-1",
                    parser=MethodVersion("fixture_parser", "1.0.0"),
                ),
            ),
            provenance=Provenance(ProvenanceKind.IMPORTED),
        )


if __name__ == "__main__":
    unittest.main()
