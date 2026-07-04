"""Tests for block-level analytics (trainingos.analytics.blocks)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trainingos.analytics.blocks import BlockMetricsReport, derive_block_metrics
from trainingos.blocks import close_block, create_block, set_phase
from trainingos.domain import (
    Activity,
    ActivityType,
    Measurement,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.storage import apply_migrations, connect_database

_TIMEZONE = "America/Toronto"
_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class BlockAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tmpdir.name) / "training.sqlite3"
        self.connection = connect_database(db_path)
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmpdir.cleanup()

    # ---- A. Happy Path -------------------------------------------------------

    def test_block_total_mileage_is_persisted_as_metric_evidence(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Marathon build",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 8, 31),
            now=_NOW,
        )
        self._activity("run-1", datetime(2026, 6, 5, 10, 0, tzinfo=UTC), 3600, 10000)
        self._activity("run-2", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), 7200, 20000)
        self._activity("run-3", datetime(2026, 6, 19, 10, 0, tzinfo=UTC), 5400, 15000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_total_mileage")
        self.assertEqual(45000.0, metric["metric_value"])
        self.assertEqual("m", metric["unit"])

    def test_block_long_run_max_records_single_largest_distance(self) -> None:
        create_block(
            self.connection,
            goal="Peak build",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 8, 31),
            now=_NOW,
        )
        self._activity("run-a", datetime(2026, 6, 5, 10, 0, tzinfo=UTC), 3600, 10000)
        self._activity("run-b", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), 7500, 21000)
        self._activity("run-c", datetime(2026, 6, 19, 10, 0, tzinfo=UTC), 2880, 8000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_long_run_max")
        self.assertEqual(21000.0, metric["metric_value"])
        self.assertEqual("m", metric["unit"])

    def test_block_average_weekly_distance_is_mean_of_complete_weeks(self) -> None:
        create_block(
            self.connection,
            goal="Three-week block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 21),
            now=_NOW,
        )
        # Mon 2026-06-01 week: 40 km
        self._activity("w1", datetime(2026, 6, 2, 10, 0, tzinfo=UTC), 14400, 40000)
        # Mon 2026-06-08 week: 50 km
        self._activity("w2", datetime(2026, 6, 9, 10, 0, tzinfo=UTC), 18000, 50000)
        # Mon 2026-06-15 week: 60 km
        self._activity("w3", datetime(2026, 6, 16, 10, 0, tzinfo=UTC), 21600, 60000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_average_weekly_distance")
        self.assertAlmostEqual(50000.0, metric["metric_value"])

    def test_block_peak_week_distance_is_highest_weekly_total(self) -> None:
        create_block(
            self.connection,
            goal="Peak week test",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 21),
            now=_NOW,
        )
        self._activity("w1", datetime(2026, 6, 2, 10, 0, tzinfo=UTC), 14400, 40000)
        self._activity("w2", datetime(2026, 6, 9, 10, 0, tzinfo=UTC), 18000, 50000)
        self._activity("w3", datetime(2026, 6, 16, 10, 0, tzinfo=UTC), 21600, 60000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_peak_week_distance")
        self.assertEqual(60000.0, metric["metric_value"])

    def test_block_recovery_coverage_ratio_counts_days_with_health_data(self) -> None:
        create_block(
            self.connection,
            goal="Recovery coverage test",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        # 7 of 14 block days have health data
        for i in range(7):
            local_date = (date(2026, 6, 1) + timedelta(days=i)).isoformat()
            self._health_day(f"health-{i}", local_date)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_recovery_coverage")
        self.assertAlmostEqual(0.5, metric["metric_value"])
        self.assertEqual("ratio", metric["unit"])

    def test_block_per_phase_mileage_produces_row_per_phase(self) -> None:
        create_block(
            self.connection,
            goal="Two-phase block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 7, 31),
            initial_phase="base",
            now=datetime(2026, 6, 1, tzinfo=UTC),
        )
        # 4 base-phase runs, 20 km each
        for week in range(4):
            run_date = datetime(2026, 6, 2, 10, 0, tzinfo=UTC) + timedelta(weeks=week)
            self._activity(f"base-{week}", run_date, 7200, 20000)

        set_phase(self.connection, "build", now=datetime(2026, 6, 29, tzinfo=UTC))

        # 4 build-phase runs, 30 km each (July)
        for week in range(4):
            run_date = datetime(2026, 6, 30, 10, 0, tzinfo=UTC) + timedelta(weeks=week)
            self._activity(f"build-{week}", run_date, 10800, 30000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        base_metric = self._metric("block_base_mileage")
        build_metric = self._metric("block_build_mileage")
        self.assertEqual(80000.0, base_metric["metric_value"])
        self.assertEqual(120000.0, build_metric["metric_value"])

    # ---- B. Boundary Value Analysis ------------------------------------------

    def test_single_activity_block_produces_all_aggregate_metrics(self) -> None:
        create_block(
            self.connection,
            goal="Single run block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 7),
            now=_NOW,
        )
        self._activity("solo", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 5400, 15000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        self.assertEqual(15000.0, self._metric("block_total_mileage")["metric_value"])
        self.assertEqual(15000.0, self._metric("block_long_run_max")["metric_value"])
        self.assertEqual(15000.0, self._metric("block_average_weekly_distance")["metric_value"])
        self.assertEqual(15000.0, self._metric("block_peak_week_distance")["metric_value"])

    def test_block_with_zero_activities_produces_no_block_distance_metrics(self) -> None:
        create_block(
            self.connection,
            goal="Empty block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 8, 31),
            now=_NOW,
        )

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        self.assertIsNone(self._optional_metric("block_total_mileage"))
        self.assertIsNone(self._optional_metric("block_long_run_max"))

    def test_block_spanning_exactly_one_week_peak_equals_average(self) -> None:
        create_block(
            self.connection,
            goal="One-week block",
            start_date=date(2026, 6, 2),
            race_date=date(2026, 6, 8),
            now=_NOW,
        )
        for i in range(5):
            self._activity(
                f"d{i}",
                datetime(2026, 6, 2 + i, 10, 0, tzinfo=UTC),
                3600,
                10000,
            )

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        avg = self._metric("block_average_weekly_distance")["metric_value"]
        peak = self._metric("block_peak_week_distance")["metric_value"]
        self.assertAlmostEqual(avg, peak)

    # ---- C. Equivalence Partitioning -----------------------------------------

    def test_activities_outside_block_date_range_are_excluded(self) -> None:
        create_block(
            self.connection,
            goal="Scoped block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 8, 31),
            now=_NOW,
        )
        self._activity("before", datetime(2026, 5, 31, 10, 0, tzinfo=UTC), 3600, 99000)
        self._activity("inside", datetime(2026, 7, 1, 10, 0, tzinfo=UTC), 3600, 10000)
        self._activity("after", datetime(2026, 9, 1, 10, 0, tzinfo=UTC), 3600, 99000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_total_mileage")
        self.assertEqual(10000.0, metric["metric_value"])

    def test_block_with_missing_distance_skips_distance_metrics_or_records_gap(self) -> None:
        create_block(
            self.connection,
            goal="Partial distance block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("with-dist", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)
        self._activity("no-dist", datetime(2026, 6, 5, 10, 0, tzinfo=UTC), 1800, None)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        total = self._optional_metric("block_total_mileage")
        if total is not None:
            # If produced despite gap, a caveat must be present
            caveats = self.connection.execute(
                "SELECT caveat FROM provenance_caveats WHERE record_id = ?",
                (total["record_id"],),
            ).fetchall()
            self.assertTrue(len(caveats) > 0, "Expected at least one caveat for missing distance")

    def test_block_with_all_distances_present_produces_complete_metric_set(self) -> None:
        create_block(
            self.connection,
            goal="Full metrics block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 28),
            now=_NOW,
        )
        for i, dist in enumerate([10000.0, 15000.0, 20000.0, 12000.0]):
            self._activity(
                f"r{i}",
                datetime(2026, 6, 2 + i * 7, 10, 0, tzinfo=UTC),
                dist / 3.0,
                dist,
            )

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        for key in (
            "block_total_mileage",
            "block_long_run_max",
            "block_average_weekly_distance",
            "block_peak_week_distance",
        ):
            self.assertIsNotNone(
                self._optional_metric(key),
                msg=f"Expected metric_evidence row for '{key}'",
            )

    def test_activities_in_non_overlapping_blocks_are_not_cross_attributed(self) -> None:
        # Create block A with two runs
        block_a_id = create_block(
            self.connection,
            goal="Block A",
            start_date=date(2026, 1, 1),
            race_date=date(2026, 3, 31),
            now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        )
        self._activity("a-run-1", datetime(2026, 1, 15, 10, 0, tzinfo=UTC), 3600, 10000)
        self._activity("a-run-2", datetime(2026, 2, 15, 10, 0, tzinfo=UTC), 3600, 15000)

        # Close block A and create block B with two different runs
        close_block(
            self.connection,
            now=datetime(2026, 3, 31, 12, 0, tzinfo=UTC),
        )
        block_b_id = create_block(
            self.connection,
            goal="Block B",
            start_date=date(2026, 4, 1),
            race_date=date(2026, 6, 30),
            now=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        )
        self._activity("b-run-1", datetime(2026, 4, 15, 10, 0, tzinfo=UTC), 3600, 20000)
        self._activity("b-run-2", datetime(2026, 5, 15, 10, 0, tzinfo=UTC), 3600, 25000)

        # Derive metrics for both blocks
        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        # Assert block A only has its activities (10000 + 15000 = 25000)
        metric_id_a = f"block_metric:block_total_mileage:1.0.0:{block_a_id}"
        row_a = self.connection.execute(
            "SELECT metric_value FROM metric_evidence WHERE record_id = ?",
            (metric_id_a,),
        ).fetchone()
        self.assertIsNotNone(row_a, "No total_mileage metric for block A")
        self.assertEqual(25000.0, row_a["metric_value"])

        # Assert block B only has its activities (20000 + 25000 = 45000)
        metric_id_b = f"block_metric:block_total_mileage:1.0.0:{block_b_id}"
        row_b = self.connection.execute(
            "SELECT metric_value FROM metric_evidence WHERE record_id = ?",
            (metric_id_b,),
        ).fetchone()
        self.assertIsNotNone(row_b, "No total_mileage metric for block B")
        self.assertEqual(45000.0, row_b["metric_value"])

    # ---- D. Edge Cases -------------------------------------------------------

    def test_derive_block_metrics_with_no_blocks_is_a_no_op(self) -> None:
        report = derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        self.assertIsInstance(report, BlockMetricsReport)
        self.assertEqual(0, report.block_count)
        self.assertEqual(0, report.metric_count)

    def test_derive_block_metrics_is_idempotent(self) -> None:
        create_block(
            self.connection,
            goal="Idempotent block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("r1", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)
        count_first = self._metric_evidence_count()

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)
        count_second = self._metric_evidence_count()

        self.assertEqual(count_first, count_second)

    def test_block_with_no_health_data_produces_zero_recovery_coverage(self) -> None:
        create_block(
            self.connection,
            goal="No health block",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("r1", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._optional_metric("block_recovery_coverage")
        if metric is not None:
            self.assertEqual(0.0, metric["metric_value"])

    def test_closed_block_analytics_computed_same_as_open_block(self) -> None:
        create_block(
            self.connection,
            goal="Closed block analytics",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("r1", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)
        close_block(self.connection, now=_NOW)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_total_mileage")
        self.assertEqual(10000.0, metric["metric_value"])

    # ---- F. Method Version ---------------------------------------------------

    def test_block_total_mileage_records_method_name_and_version_in_records_table(self) -> None:
        create_block(
            self.connection,
            goal="Method version test",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("r1", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_total_mileage")
        record = self.connection.execute(
            "SELECT provenance_kind, method_name, method_version FROM records WHERE record_id = ?",
            (metric["record_id"],),
        ).fetchone()
        self.assertEqual("computed", record["provenance_kind"])
        self.assertEqual("block_total_mileage", record["method_name"])
        self.assertTrue(record["method_version"].strip())

    def test_all_block_metric_records_have_computed_provenance(self) -> None:
        create_block(
            self.connection,
            goal="Provenance check",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("r1", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        rows = self.connection.execute(
            """
            SELECT me.record_id, r.provenance_kind
            FROM metric_evidence AS me
            JOIN records AS r USING (record_id)
            WHERE me.metric_key LIKE 'block_%'
            """
        ).fetchall()
        self.assertTrue(len(rows) > 0, "Expected at least one block metric_evidence row")
        for row in rows:
            self.assertEqual("computed", row["provenance_kind"], f"record_id={row['record_id']}")

    def test_metric_evidence_rows_reference_activity_record_ids_as_evidence(self) -> None:
        create_block(
            self.connection,
            goal="Evidence check",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 6, 14),
            now=_NOW,
        )
        self._activity("run-a", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), 3600, 10000)
        self._activity("run-b", datetime(2026, 6, 5, 10, 0, tzinfo=UTC), 3600, 8000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        metric = self._metric("block_total_mileage")
        evidence = self.connection.execute(
            """
            SELECT evidence_record_id
            FROM provenance_evidence
            WHERE record_id = ?
            ORDER BY position
            """,
            (metric["record_id"],),
        ).fetchall()
        evidence_ids = {row["evidence_record_id"] for row in evidence}
        self.assertIn("run-a", evidence_ids)
        self.assertIn("run-b", evidence_ids)

    # ---- G. Prior-Block Comparison (Two-Block Fixture) -----------------------

    def test_prior_block_comparison_metric_exists_for_second_block(self) -> None:
        # Block A: Jan–Mar 2026
        create_block(
            self.connection,
            goal="Block A",
            start_date=date(2026, 1, 1),
            race_date=date(2026, 3, 31),
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for week in range(12):
            self._activity(
                f"a-{week}",
                datetime(2026, 1, 5, 10, 0, tzinfo=UTC) + timedelta(weeks=week),
                9000,
                25000,
            )
        close_block(self.connection, now=datetime(2026, 3, 31, tzinfo=UTC))

        # Block B: Apr–Jun 2026
        create_block(
            self.connection,
            goal="Block B",
            start_date=date(2026, 4, 1),
            race_date=date(2026, 6, 30),
            now=datetime(2026, 4, 1, tzinfo=UTC),
        )
        for week in range(12):
            self._activity(
                f"b-{week}",
                datetime(2026, 4, 6, 10, 0, tzinfo=UTC) + timedelta(weeks=week),
                10800,
                33000,
            )

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        comparison = self._optional_metric("block_total_mileage_vs_prior")
        self.assertIsNotNone(
            comparison,
            "Expected a prior-block comparison metric_evidence row for block B",
        )

    def test_prior_block_missing_produces_no_comparison_metric_or_explicit_gap(self) -> None:
        create_block(
            self.connection,
            goal="Only block",
            start_date=date(2026, 1, 1),
            race_date=date(2026, 3, 31),
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self._activity("r1", datetime(2026, 1, 5, 10, 0, tzinfo=UTC), 3600, 10000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        comparison = self._optional_metric("block_total_mileage_vs_prior")
        if comparison is not None:
            caveats = self.connection.execute(
                "SELECT caveat FROM provenance_caveats WHERE record_id = ?",
                (comparison["record_id"],),
            ).fetchall()
            caveat_texts = [row["caveat"] for row in caveats]
            self.assertTrue(
                any("prior" in c for c in caveat_texts),
                f"Expected a prior-block caveat, got: {caveat_texts}",
            )

    def test_prior_block_long_run_max_comparison_references_correct_blocks(self) -> None:
        create_block(
            self.connection,
            goal="Block A long run",
            start_date=date(2026, 1, 1),
            race_date=date(2026, 3, 31),
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self._activity("a-long", datetime(2026, 1, 5, 10, 0, tzinfo=UTC), 9000, 25000)
        close_block(self.connection, now=datetime(2026, 3, 31, tzinfo=UTC))

        create_block(
            self.connection,
            goal="Block B long run",
            start_date=date(2026, 4, 1),
            race_date=date(2026, 6, 30),
            now=datetime(2026, 4, 1, tzinfo=UTC),
        )
        self._activity("b-long", datetime(2026, 4, 5, 10, 0, tzinfo=UTC), 10800, 28000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)

        comparison = self._optional_metric("block_long_run_max_vs_prior")
        self.assertIsNotNone(comparison, "Expected long-run comparison metric for block B")

    def test_two_block_fixture_derive_is_idempotent(self) -> None:
        create_block(
            self.connection,
            goal="Idempotent A",
            start_date=date(2026, 1, 1),
            race_date=date(2026, 3, 31),
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self._activity("a1", datetime(2026, 1, 5, 10, 0, tzinfo=UTC), 3600, 10000)
        close_block(self.connection, now=datetime(2026, 3, 31, tzinfo=UTC))

        create_block(
            self.connection,
            goal="Idempotent B",
            start_date=date(2026, 4, 1),
            race_date=date(2026, 6, 30),
            now=datetime(2026, 4, 1, tzinfo=UTC),
        )
        self._activity("b1", datetime(2026, 4, 5, 10, 0, tzinfo=UTC), 3600, 12000)

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)
        count_first = self._metric_evidence_count()

        derive_block_metrics(self.connection, timezone=_TIMEZONE, now=_NOW)
        count_second = self._metric_evidence_count()

        self.assertEqual(count_first, count_second)

    # ---- Helpers -------------------------------------------------------------

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

    def _health_day(self, record_id: str, local_date: str) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'daily_health', ?, ?, ?, 'user_entered')
            """,
            (record_id, _TIMEZONE, _NOW.isoformat(), _NOW.isoformat()),
        )
        self.connection.execute(
            "INSERT INTO daily_health (record_id, local_date) VALUES (?, ?)",
            (record_id, local_date),
        )
        self.connection.execute(
            """
            INSERT INTO metric_values (
                owner_record_id, metric_key, metric_status, metric_value, unit
            ) VALUES (?, 'sleep_duration', 'measured', 28800, 's')
            """,
            (record_id,),
        )

    def _metadata(self, record_id: str) -> RecordMetadata:
        return RecordMetadata(
            record_id=record_id,
            timezone=_TIMEZONE,
            created_at=_NOW,
            updated_at=_NOW,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        )

    def _metric(self, metric_key: str) -> sqlite3.Row:
        row = self._optional_metric(metric_key)
        self.assertIsNotNone(row, f"Expected metric_evidence row for '{metric_key}'")
        return row  # type: ignore[return-value]

    def _optional_metric(self, metric_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT record_id, metric_value, unit, quality FROM metric_evidence WHERE metric_key = ?",
            (metric_key,),
        ).fetchone()

    def _metric_evidence_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM metric_evidence").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
