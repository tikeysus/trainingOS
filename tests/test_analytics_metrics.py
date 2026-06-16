from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trainingos.analytics import ProjectionStatus, RaceProjection, derive_training_metrics
from trainingos.domain import (
    Activity,
    ActivitySample,
    ActivityType,
    Lap,
    Measurement,
    MethodVersion,
    MetricValue,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.storage import apply_migrations, connect_database


class AnalyticsMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        self.connection = connect_database(database_path)
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_weekly_summary_and_metric_evidence_are_hand_calculated(self) -> None:
        self._activity("run-1", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 3600, 10000)
        self._activity("run-2", datetime(2026, 11, 5, 11, 0, tzinfo=UTC), 1800, 5000)

        report = derive_training_metrics(
            self.connection,
            now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        )

        self.assertEqual(1, report.week_count)
        summary = self.connection.execute(
            """
            SELECT run_count, distance_metres, duration_seconds, long_run_metres,
                   average_pace_seconds_per_kilometre, data_gaps_json
            FROM weekly_summaries
            WHERE week_start_date = '2026-11-02'
            """
        ).fetchone()
        self.assertEqual(2, summary["run_count"])
        self.assertEqual(15000.0, summary["distance_metres"])
        self.assertEqual(5400.0, summary["duration_seconds"])
        self.assertEqual(10000.0, summary["long_run_metres"])
        self.assertEqual(360.0, summary["average_pace_seconds_per_kilometre"])
        self.assertEqual("[]", summary["data_gaps_json"])
        metric = self._metric("weekly_distance")
        self.assertEqual(15000.0, metric["metric_value"])
        self.assertEqual("m", metric["unit"])
        self.assertEqual(("computed", "weekly_distance", "1.0.0"), self._record(metric["record_id"]))
        self.assertEqual(2, self._evidence_count(metric["record_id"]))

    def test_timezone_assigns_utc_boundary_activity_to_local_week(self) -> None:
        self._activity("late-sunday", datetime(2026, 11, 2, 3, 30, tzinfo=UTC), 1200, 3000)

        derive_training_metrics(
            self.connection,
            timezone="America/Toronto",
            now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        )

        summary = self.connection.execute(
            "SELECT week_start_date FROM weekly_summaries"
        ).fetchone()
        self.assertEqual("2026-10-26", summary["week_start_date"])

    def test_repeated_derivation_is_idempotent(self) -> None:
        self._activity("run-1", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 3600, 10000)

        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 12, tzinfo=UTC))
        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 13, tzinfo=UTC))

        self.assertEqual(1, self._count("weekly_summaries"))
        self.assertEqual(
            6,
            self.connection.execute(
                "SELECT COUNT(*) FROM metric_evidence"
            ).fetchone()[0],
        )

    def test_load_trend_compares_with_prior_complete_weeks(self) -> None:
        self._activity("week-1", datetime(2026, 10, 19, 11, 0, tzinfo=UTC), 3600, 10000)
        self._activity("week-2", datetime(2026, 10, 26, 11, 0, tzinfo=UTC), 3600, 20000)
        self._activity("week-3", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 3600, 30000)

        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 12, tzinfo=UTC))

        trends = self.connection.execute(
            """
            SELECT metric_value, quality
            FROM metric_evidence
            WHERE metric_key = 'training_load_trend'
            ORDER BY window_start
            """
        ).fetchall()
        self.assertEqual(2, len(trends))
        self.assertEqual(2.0, trends[0]["metric_value"])
        self.assertEqual(2.0, trends[1]["metric_value"])
        self.assertEqual(1.0, trends[1]["quality"])

    def test_marathon_pace_volume_uses_matching_laps_as_evidence(self) -> None:
        self._activity("run-1", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 1800, 6000)
        self._lap("lap-match", "run-1", 0, 270, 1000)
        self._lap("lap-easy", "run-1", 1, 360, 1000)

        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 12, tzinfo=UTC))

        metric = self._metric("marathon_pace_volume")
        self.assertEqual(1000.0, metric["metric_value"])
        self.assertEqual(1, self._evidence_count(metric["record_id"]))
        evidence = self.connection.execute(
            """
            SELECT evidence_record_id
            FROM provenance_evidence
            WHERE record_id = ?
            """,
            (metric["record_id"],),
        ).fetchone()
        self.assertEqual("lap-match", evidence["evidence_record_id"])

    def test_missing_distance_records_gap_and_skips_distance_metrics(self) -> None:
        self._activity("run-1", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 3600, None)

        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 12, tzinfo=UTC))

        summary = self.connection.execute(
            "SELECT data_gaps_json FROM weekly_summaries"
        ).fetchone()
        self.assertIn("one_or_more_runs_missing_distance", summary["data_gaps_json"])
        self.assertIsNone(self._optional_metric("weekly_distance"))
        caveat_count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM provenance_caveats
            WHERE caveat = 'one_or_more_runs_missing_distance'
            """
        ).fetchone()[0]
        self.assertEqual(1, caveat_count)

    def test_aerobic_efficiency_and_heart_rate_drift_use_samples(self) -> None:
        self._activity("run-1", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 1000, 4000)
        for index, heart_rate in enumerate((100, 100, 110, 110)):
            self._sample(
                f"sample-{index}",
                "run-1",
                datetime(2026, 11, 2, 11, index, tzinfo=UTC),
                heart_rate,
            )

        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 12, tzinfo=UTC))

        efficiency = self._metric("aerobic_efficiency")
        self.assertAlmostEqual(4 / 105, efficiency["metric_value"])
        self.assertEqual("ratio", efficiency["unit"])
        drift = self._metric("heart_rate_drift")
        self.assertEqual(10.0, drift["metric_value"])
        self.assertEqual("%", drift["unit"])

    def test_recovery_coverage_tracks_missing_health_days(self) -> None:
        self._activity("run-1", datetime(2026, 11, 2, 11, 0, tzinfo=UTC), 1000, 4000)
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (
                'health-1', 'daily_health', 'America/Toronto',
                '2026-11-02T12:00:00+00:00',
                '2026-11-02T12:00:00+00:00',
                'user_entered'
            )
            """
        )
        self.connection.execute(
            "INSERT INTO daily_health (record_id, local_date) VALUES ('health-1', '2026-11-02')"
        )
        self.connection.execute(
            """
            INSERT INTO metric_values (
                owner_record_id, metric_key, metric_status, metric_value, unit
            ) VALUES ('health-1', 'sleep_duration', 'measured', 28800, 's')
            """
        )

        derive_training_metrics(self.connection, now=datetime(2026, 11, 9, 12, tzinfo=UTC))

        recovery = self._metric("recovery_coverage")
        self.assertAlmostEqual(1 / 7, recovery["metric_value"])
        self.assertEqual("ratio", recovery["unit"])
        caveats = self.connection.execute(
            """
            SELECT caveat
            FROM provenance_caveats
            WHERE record_id = ?
            """,
            (recovery["record_id"],),
        ).fetchall()
        self.assertEqual(["recovery coverage is incomplete"], [row["caveat"] for row in caveats])

    def test_race_projection_contract_requires_evidence_or_data_gap(self) -> None:
        projection = RaceProjection(
            method=MethodVersion("race_projection", "1.0.0"),
            status=ProjectionStatus.ESTIMATED,
            target_race_id="hamilton-2026",
            projected_duration_seconds=11400,
            uncertainty_seconds=300,
            evidence_record_ids=("weekly_summary:2026-11-02",),
            caveats=("Projection is informational, not a guarantee",),
        )

        self.assertEqual(ProjectionStatus.ESTIMATED, projection.status)

        with self.assertRaisesRegex(ValueError, "requires evidence"):
            RaceProjection(
                method=projection.method,
                status=ProjectionStatus.ESTIMATED,
                target_race_id="hamilton-2026",
                projected_duration_seconds=11400,
                uncertainty_seconds=300,
                evidence_record_ids=(),
                caveats=(),
            )

        with self.assertRaisesRegex(ValueError, "requires caveats"):
            RaceProjection(
                method=projection.method,
                status=ProjectionStatus.INSUFFICIENT_DATA,
                target_race_id="hamilton-2026",
                projected_duration_seconds=None,
                uncertainty_seconds=None,
                evidence_record_ids=(),
                caveats=(),
            )

    def _activity(
        self,
        record_id: str,
        started_at: datetime,
        duration_seconds: float,
        distance_metres: float | None,
    ) -> None:
        self.store.upsert_activity(
            Activity(
                metadata=self._metadata(record_id),
                activity_type=ActivityType.RUN,
                started_at=started_at,
                duration=Measurement(duration_seconds, Unit.SECOND),
                distance=None
                if distance_metres is None
                else Measurement(distance_metres, Unit.METRE),
            )
        )

    def _sample(
        self,
        record_id: str,
        activity_id: str,
        recorded_at: datetime,
        heart_rate: float,
    ) -> None:
        self.store.upsert_sample(
            ActivitySample(
                metadata=self._metadata(record_id),
                activity_id=activity_id,
                recorded_at=recorded_at,
                metrics=(
                    MetricValue(
                        "heart_rate",
                        Measurement(heart_rate, Unit.BEAT_PER_MINUTE),
                    ),
                ),
            )
        )

    def _lap(
        self,
        record_id: str,
        activity_id: str,
        index: int,
        duration_seconds: float,
        distance_metres: float,
    ) -> None:
        self.store.upsert_lap(
            Lap(
                metadata=self._metadata(record_id),
                activity_id=activity_id,
                index=index,
                started_at=datetime(2026, 11, 2, 11, index, tzinfo=UTC),
                duration=Measurement(duration_seconds, Unit.SECOND),
                distance=Measurement(distance_metres, Unit.METRE),
            )
        )

    def _metadata(self, record_id: str) -> RecordMetadata:
        timestamp = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        return RecordMetadata(
            record_id=record_id,
            timezone="America/Toronto",
            created_at=timestamp,
            updated_at=timestamp,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        )

    def _metric(self, metric_key: str) -> sqlite3.Row:
        row = self._optional_metric(metric_key)
        self.assertIsNotNone(row)
        return row

    def _optional_metric(self, metric_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT record_id, metric_value, unit, quality
            FROM metric_evidence
            WHERE metric_key = ?
            """,
            (metric_key,),
        ).fetchone()

    def _record(self, record_id: str) -> tuple[str, str, str]:
        row = self.connection.execute(
            """
            SELECT provenance_kind, method_name, method_version
            FROM records
            WHERE record_id = ?
            """,
            (record_id,),
        ).fetchone()
        return tuple(row)

    def _evidence_count(self, record_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM provenance_evidence WHERE record_id = ?",
            (record_id,),
        ).fetchone()[0]

    def _count(self, table: str) -> int:
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
