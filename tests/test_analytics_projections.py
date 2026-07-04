"""Tests for race projection formulas: Riegel and training-load.

Assumes the implementation will add:
  - analytics.projections.derive_race_projections(connection, *, now, lookback_days)
  - analytics.projections.RIEGEL_PROJECTION_METHOD: MethodVersion
  - analytics.projections.TRAINING_LOAD_PROJECTION_METHOD: MethodVersion
  - migration adding `races` and `race_projections` tables

Schema assumed for `races`:
    record_id TEXT PK, name TEXT, started_at TEXT, distance_metres REAL,
    result_duration_seconds REAL, target_duration_seconds REAL

Schema assumed for `race_projections`:
    record_id TEXT PK, target_race_id TEXT, status TEXT,
    projected_duration_seconds REAL, uncertainty_seconds REAL
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trainingos.analytics import derive_race_projections
from trainingos.analytics.projections import (
    RIEGEL_PROJECTION_METHOD,
    TRAINING_LOAD_PROJECTION_METHOD,
)
from trainingos.storage import apply_migrations, connect_database

MARATHON_METRES = 42195.0
TARGET_RACE_ID = "marathon-hamilton-2026"
NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


class RaceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        self.connection = connect_database(database_path)
        apply_migrations(self.connection)
        self._insert_target_race()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    # =========================================================================
    # Riegel: A. Happy path
    # =========================================================================

    def test_riegel_projects_marathon_from_5k_result(self) -> None:
        # T2 = 1200 × (42195/5000)^1.06 ≈ 11 509 s  (~3:11:49)
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("estimated", row["status"])
        self.assertAlmostEqual(11509.0, row["projected_duration_seconds"], delta=10.0)
        self.assertGreaterEqual(row["uncertainty_seconds"], 0.0)
        self.assertIn("5k", self._evidence_ids(row["record_id"]))

    def test_riegel_projects_marathon_from_half_marathon_result(self) -> None:
        # T2 = 5400 × 2.0^1.06 ≈ 11 259 s  (~3:07:39)
        self._source_race("hm", 21097.5, 5400.0, days_ago=60)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("estimated", row["status"])
        self.assertAlmostEqual(11259.0, row["projected_duration_seconds"], delta=10.0)

    # =========================================================================
    # Riegel: B. Boundary values
    # =========================================================================

    def test_riegel_includes_race_at_exact_lookback_boundary(self) -> None:
        self._source_race("boundary", 5000.0, 1200.0, days_ago=365)

        derive_race_projections(self.connection, now=NOW, lookback_days=365)

        self.assertEqual("estimated", self._projection(RIEGEL_PROJECTION_METHOD.name)["status"])

    def test_riegel_excludes_race_one_day_beyond_lookback(self) -> None:
        self._source_race("old", 5000.0, 1200.0, days_ago=366)

        derive_race_projections(self.connection, now=NOW, lookback_days=365)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("insufficient_data", row["status"])
        self.assertIsNone(row["projected_duration_seconds"])
        self.assertTrue(self._caveats(row["record_id"]))

    def test_riegel_same_distance_source_and_target_preserves_result_time(self) -> None:
        # D2/D1 = 1, so T2 == T1 regardless of exponent.
        self._source_race("mara-result", MARATHON_METRES, 11400.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("estimated", row["status"])
        self.assertAlmostEqual(11400.0, row["projected_duration_seconds"], delta=1.0)

    # =========================================================================
    # Riegel: C. Equivalence partitioning
    # =========================================================================

    def test_riegel_uses_most_recent_of_multiple_qualifying_races(self) -> None:
        self._source_race("old-5k", 5000.0, 1300.0, days_ago=180)
        self._source_race("recent-5k", 5000.0, 1200.0, days_ago=30)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        evidence = self._evidence_ids(row["record_id"])
        self.assertIn("recent-5k", evidence)
        self.assertNotIn("old-5k", evidence)

    def test_riegel_skips_race_with_no_result_duration(self) -> None:
        self._source_race_no_result("no-result", 5000.0, days_ago=60)

        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(
            "insufficient_data",
            self._projection(RIEGEL_PROJECTION_METHOD.name)["status"],
        )

    def test_riegel_excludes_future_race_even_with_result(self) -> None:
        self._source_race_at("future", 5000.0, 1200.0, "2026-12-01T08:00:00+00:00")

        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(
            "insufficient_data",
            self._projection(RIEGEL_PROJECTION_METHOD.name)["status"],
        )

    # =========================================================================
    # Riegel: D. Edge cases
    # =========================================================================

    def test_riegel_returns_insufficient_data_when_no_source_races(self) -> None:
        # Only the target race is in the DB; no historical results.
        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("insufficient_data", row["status"])
        self.assertIsNone(row["projected_duration_seconds"])
        self.assertIsNone(row["uncertainty_seconds"])
        self.assertTrue(self._caveats(row["record_id"]))

    def test_riegel_configurable_lookback_excludes_otherwise_valid_race(self) -> None:
        self._source_race("mid-range", 5000.0, 1200.0, days_ago=60)

        derive_race_projections(self.connection, now=NOW, lookback_days=30)

        self.assertEqual(
            "insufficient_data",
            self._projection(RIEGEL_PROJECTION_METHOD.name)["status"],
        )

    # =========================================================================
    # Riegel: E. Error / negative
    # =========================================================================

    def test_riegel_insufficient_data_has_no_duration_or_uncertainty(self) -> None:
        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("insufficient_data", row["status"])
        self.assertIsNone(row["projected_duration_seconds"])
        self.assertIsNone(row["uncertainty_seconds"])

    def test_riegel_estimated_uncertainty_is_non_negative(self) -> None:
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual("estimated", row["status"])
        self.assertGreaterEqual(row["uncertainty_seconds"], 0.0)

    # =========================================================================
    # Riegel: F. Persistence / state
    # =========================================================================

    def test_riegel_projection_persisted_with_correct_method_version(self) -> None:
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertEqual(RIEGEL_PROJECTION_METHOD.name, row["method_name"])
        self.assertEqual(RIEGEL_PROJECTION_METHOD.version, row["method_version"])

    def test_riegel_projection_is_idempotent(self) -> None:
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)
        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(1, self._projection_count(RIEGEL_PROJECTION_METHOD.name))

    def test_riegel_evidence_ids_point_to_source_race_record(self) -> None:
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertIn("5k", self._evidence_ids(row["record_id"]))

    def test_riegel_record_id_encodes_method_version(self) -> None:
        # Changing method_version must produce a distinct record_id so stale
        # v1 results are not silently overwritten by v2 runs.
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(RIEGEL_PROJECTION_METHOD.name)
        self.assertIn(RIEGEL_PROJECTION_METHOD.version, row["record_id"])

    # =========================================================================
    # Training-load: A. Happy path
    # =========================================================================

    def test_training_load_projects_marathon_from_sufficient_weeks(self) -> None:
        self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertIsNotNone(row)
        self.assertEqual("estimated", row["status"])
        self.assertGreater(row["projected_duration_seconds"], 0.0)
        # Sanity bounds: 2 h to 6 h for a realistic training load.
        self.assertGreater(row["projected_duration_seconds"], 7200.0)
        self.assertLess(row["projected_duration_seconds"], 21600.0)
        self.assertGreaterEqual(row["uncertainty_seconds"], 0.0)

    def test_training_load_evidence_ids_cite_metric_evidence_records(self) -> None:
        inserted_ids = self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        evidence = self._evidence_ids(row["record_id"])
        for expected in inserted_ids:
            self.assertIn(expected, evidence)

    # =========================================================================
    # Training-load: B. Boundary values
    # =========================================================================

    def test_training_load_at_minimum_week_threshold_gives_estimated(self) -> None:
        self._weeks(count=4)

        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(
            "estimated",
            self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)["status"],
        )

    def test_training_load_one_fewer_than_minimum_gives_insufficient_data(self) -> None:
        self._weeks(count=3)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertEqual("insufficient_data", row["status"])
        self.assertTrue(self._caveats(row["record_id"]))

    def test_training_load_zero_weekly_distance_gives_insufficient_data(self) -> None:
        for i in range(6):
            ws, we = self._week_window(i + 1)
            self._metric(f"dist-zero-{i}", "weekly_distance", 0.0, "m", ws, we)

        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(
            "insufficient_data",
            self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)["status"],
        )

    # =========================================================================
    # Training-load: C. Equivalence partitioning
    # =========================================================================

    def test_training_load_partial_weeks_add_caveat_and_remain_estimated(self) -> None:
        # 4 complete weeks; 2 weeks missing weekly_distance.
        self._weeks(count=4)
        for i in range(4, 6):
            ws, we = self._week_window(i + 1)
            self._metric(f"long-partial-{i}", "weekly_long_run", 25000.0, "m", ws, we)
            self._metric(f"pace-partial-{i}", "weekly_average_pace", 330.0, "s/km", ws, we)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertEqual("estimated", row["status"])
        caveats = self._caveats(row["record_id"])
        self.assertTrue(
            any("partial" in c or "missing" in c or "distance" in c for c in caveats),
            f"expected a partial-data caveat, got: {caveats}",
        )

    def test_training_load_missing_long_run_metric_adds_caveat(self) -> None:
        self._weeks(count=4, long_run=False)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertTrue(self._caveats(row["record_id"]))

    def test_training_load_missing_average_pace_metric_adds_caveat(self) -> None:
        self._weeks(count=4, avg_pace=False)

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertTrue(self._caveats(row["record_id"]))

    # =========================================================================
    # Training-load: D. Edge cases
    # =========================================================================

    def test_training_load_returns_insufficient_data_when_no_metric_evidence(self) -> None:
        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertEqual("insufficient_data", row["status"])
        self.assertIsNone(row["projected_duration_seconds"])
        self.assertIsNone(row["uncertainty_seconds"])
        self.assertTrue(self._caveats(row["record_id"]))

    def test_training_load_single_week_gives_insufficient_data(self) -> None:
        self._weeks(count=1)

        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(
            "insufficient_data",
            self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)["status"],
        )

    # =========================================================================
    # Training-load: E. Error / negative
    # =========================================================================

    def test_training_load_insufficient_data_has_no_duration_or_uncertainty(self) -> None:
        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertEqual("insufficient_data", row["status"])
        self.assertIsNone(row["projected_duration_seconds"])
        self.assertIsNone(row["uncertainty_seconds"])

    def test_training_load_projected_duration_is_positive(self) -> None:
        self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertEqual("estimated", row["status"])
        self.assertGreater(row["projected_duration_seconds"], 0.0)

    # =========================================================================
    # Training-load: F. Persistence / state
    # =========================================================================

    def test_training_load_persisted_with_correct_method_version(self) -> None:
        self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertEqual(TRAINING_LOAD_PROJECTION_METHOD.name, row["method_name"])
        self.assertEqual(TRAINING_LOAD_PROJECTION_METHOD.version, row["method_version"])

    def test_training_load_projection_is_idempotent(self) -> None:
        self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)
        derive_race_projections(self.connection, now=NOW)

        self.assertEqual(1, self._projection_count(TRAINING_LOAD_PROJECTION_METHOD.name))

    def test_training_load_record_id_encodes_method_version(self) -> None:
        self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)

        row = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertIn(TRAINING_LOAD_PROJECTION_METHOD.version, row["record_id"])

    # =========================================================================
    # Integration: pipeline-level tests
    # =========================================================================

    def test_both_methods_produce_separate_projections_for_same_race(self) -> None:
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)
        self._eight_weeks()

        derive_race_projections(self.connection, now=NOW)

        riegel = self._projection(RIEGEL_PROJECTION_METHOD.name)
        load = self._projection(TRAINING_LOAD_PROJECTION_METHOD.name)
        self.assertIsNotNone(riegel)
        self.assertIsNotNone(load)
        self.assertNotEqual(riegel["record_id"], load["record_id"])

    def test_no_target_races_produces_zero_projections_no_error(self) -> None:
        self.connection.execute(
            "DELETE FROM races WHERE record_id = ?", (TARGET_RACE_ID,)
        )
        self.connection.execute(
            "DELETE FROM records WHERE record_id = ?", (TARGET_RACE_ID,)
        )

        derive_race_projections(self.connection, now=NOW)

        total = self.connection.execute(
            "SELECT COUNT(*) FROM race_projections"
        ).fetchone()[0]
        self.assertEqual(0, total)

    def test_derive_returns_count_of_projections_computed(self) -> None:
        self._source_race("5k", 5000.0, 1200.0, days_ago=90)
        self._eight_weeks()

        count = derive_race_projections(self.connection, now=NOW)

        # One Riegel + one training-load projection for the single target race.
        self.assertEqual(2, count)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _insert_target_race(self) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'race', 'America/Toronto',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                'user_entered')
            """,
            (TARGET_RACE_ID,),
        )
        self.connection.execute(
            """
            INSERT INTO races (record_id, name, started_at, distance_metres)
            VALUES (?, 'Hamilton Marathon 2026', '2026-10-18T08:00:00+00:00', ?)
            """,
            (TARGET_RACE_ID, MARATHON_METRES),
        )

    def _source_race(
        self,
        race_id: str,
        distance_metres: float,
        result_duration_seconds: float,
        days_ago: int,
    ) -> None:
        started_at = (NOW - timedelta(days=days_ago)).isoformat()
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'race', 'America/Toronto', ?, ?, 'user_entered')
            """,
            (race_id, started_at, started_at),
        )
        self.connection.execute(
            """
            INSERT INTO races (
                record_id, name, started_at, distance_metres,
                result_duration_seconds
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (race_id, f"Race {race_id}", started_at, distance_metres,
             result_duration_seconds),
        )

    def _source_race_no_result(
        self,
        race_id: str,
        distance_metres: float,
        days_ago: int,
    ) -> None:
        started_at = (NOW - timedelta(days=days_ago)).isoformat()
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'race', 'America/Toronto', ?, ?, 'user_entered')
            """,
            (race_id, started_at, started_at),
        )
        self.connection.execute(
            """
            INSERT INTO races (record_id, name, started_at, distance_metres)
            VALUES (?, ?, ?, ?)
            """,
            (race_id, f"Race {race_id}", started_at, distance_metres),
        )

    def _source_race_at(
        self,
        race_id: str,
        distance_metres: float,
        result_duration_seconds: float,
        started_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'race', 'America/Toronto',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                'user_entered')
            """,
            (race_id,),
        )
        self.connection.execute(
            """
            INSERT INTO races (
                record_id, name, started_at, distance_metres,
                result_duration_seconds
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (race_id, f"Race {race_id}", started_at, distance_metres,
             result_duration_seconds),
        )

    def _week_window(self, weeks_back: int) -> tuple[str, str]:
        week_start = (NOW - timedelta(weeks=weeks_back)).date()
        ws = f"{week_start.isoformat()}T00:00:00+00:00"
        we = f"{(week_start + timedelta(days=6)).isoformat()}T23:59:59+00:00"
        return ws, we

    def _metric(
        self,
        record_id: str,
        metric_key: str,
        value: float,
        unit: str,
        window_start: str,
        window_end: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind, method_name, method_version
            ) VALUES (?, 'metric_evidence', 'America/Toronto',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                'computed', ?, '1.0.0')
            """,
            (record_id, metric_key),
        )
        self.connection.execute(
            """
            INSERT INTO metric_evidence (
                record_id, metric_key, metric_value, unit,
                window_start, window_end, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (record_id, metric_key, value, unit, window_start, window_end,
             f"{metric_key}={value}"),
        )

    def _weeks(
        self,
        count: int,
        long_run: bool = True,
        avg_pace: bool = True,
    ) -> list[str]:
        inserted: list[str] = []
        for i in range(count):
            ws, we = self._week_window(i + 1)
            dist_id = f"dist-{i}"
            self._metric(dist_id, "weekly_distance", 60000.0, "m", ws, we)
            inserted.append(dist_id)
            if long_run:
                lr_id = f"long-{i}"
                self._metric(lr_id, "weekly_long_run", 28000.0, "m", ws, we)
                inserted.append(lr_id)
            if avg_pace:
                pace_id = f"pace-{i}"
                self._metric(pace_id, "weekly_average_pace", 330.0, "s/km", ws, we)
                inserted.append(pace_id)
        return inserted

    def _eight_weeks(self) -> list[str]:
        return self._weeks(count=8)

    def _projection(self, method_name: str):
        return self.connection.execute(
            """
            SELECT rp.record_id, rp.target_race_id, rp.status,
                   rp.projected_duration_seconds, rp.uncertainty_seconds,
                   r.method_name, r.method_version
            FROM race_projections rp
            JOIN records r ON r.record_id = rp.record_id
            WHERE rp.target_race_id = ? AND r.method_name = ?
            """,
            (TARGET_RACE_ID, method_name),
        ).fetchone()

    def _projection_count(self, method_name: str) -> int:
        return self.connection.execute(
            """
            SELECT COUNT(*) FROM race_projections rp
            JOIN records r ON r.record_id = rp.record_id
            WHERE r.method_name = ?
            """,
            (method_name,),
        ).fetchone()[0]

    def _evidence_ids(self, record_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT evidence_record_id
            FROM provenance_evidence
            WHERE record_id = ?
            ORDER BY position
            """,
            (record_id,),
        ).fetchall()
        return [row["evidence_record_id"] for row in rows]

    def _caveats(self, record_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT caveat
            FROM provenance_caveats
            WHERE record_id = ?
            ORDER BY position
            """,
            (record_id,),
        ).fetchall()
        return [row["caveat"] for row in rows]


if __name__ == "__main__":
    unittest.main()
