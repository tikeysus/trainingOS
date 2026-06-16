from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trainingos.storage import apply_migrations, connect_database


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = ROOT / "grafana/dashboards/trainingos-local-dashboard.json"
DATASOURCE_PATH = ROOT / "grafana/provisioning/datasources/trainingos-sqlite.yml"
COMPOSE_PATH = ROOT / "docker-compose.yml"


class DashboardReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.connection = connect_database(
            Path(self.temporary_directory.name) / "training.sqlite3"
        )
        apply_migrations(self.connection)
        self._seed_dashboard_data()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_weekly_and_metric_views_expose_units_versions_and_caveats(self) -> None:
        weekly = self.connection.execute(
            """
            SELECT distance_kilometres, long_run_kilometres, method_name,
                   method_version, data_gaps_json, caveats
            FROM dashboard_weekly_training
            WHERE record_id = 'weekly-current-1'
            """
        ).fetchone()
        self.assertEqual(42.0, weekly["distance_kilometres"])
        self.assertEqual(21.0, weekly["long_run_kilometres"])
        self.assertEqual(("weekly_summary", "1.0.0"), tuple(weekly[2:4]))
        self.assertIn("missing_sleep", weekly["data_gaps_json"])
        self.assertIn("recovery coverage is incomplete", weekly["caveats"])

        metric = self.connection.execute(
            """
            SELECT metric_value, unit, quality, method_name, method_version,
                   caveats, evidence_record_ids
            FROM dashboard_metric_timeseries
            WHERE metric_key = 'recovery_coverage'
            """
        ).fetchone()
        self.assertEqual(0.5, metric["metric_value"])
        self.assertEqual("ratio", metric["unit"])
        self.assertEqual(0.5, metric["quality"])
        self.assertEqual(("recovery_coverage", "1.0.0"), tuple(metric[3:5]))
        self.assertIn("recovery coverage is incomplete", metric["caveats"])
        self.assertIn("activity-current-1", metric["evidence_record_ids"])

    def test_block_comparison_and_projection_views_handle_missing_data(self) -> None:
        comparison = self.connection.execute(
            """
            SELECT current_block_name, previous_block_name,
                   current_distance_kilometres, previous_distance_kilometres,
                   distance_delta_metres, caveat
            FROM dashboard_block_comparison
            """
        ).fetchone()
        self.assertEqual("Current Hamilton build", comparison["current_block_name"])
        self.assertEqual("Prior base block", comparison["previous_block_name"])
        self.assertEqual(42.0, comparison["current_distance_kilometres"])
        self.assertEqual(30.0, comparison["previous_distance_kilometres"])
        self.assertEqual(12000.0, comparison["distance_delta_metres"])
        self.assertIsNone(comparison["caveat"])

        projection = self.connection.execute(
            """
            SELECT projection_status, projected_duration_seconds,
                   uncertainty_seconds, caveat
            FROM dashboard_race_projection_status
            WHERE race_id = 'race-current'
            """
        ).fetchone()
        self.assertEqual("unavailable", projection["projection_status"])
        self.assertIsNone(projection["projected_duration_seconds"])
        self.assertIsNone(projection["uncertainty_seconds"])
        self.assertIn("not been persisted", projection["caveat"])

    def test_evidence_and_annotation_views_are_local_and_inspectable(self) -> None:
        evidence = self.connection.execute(
            """
            SELECT document_type, title, evidence_json, caveats_json, stale_reason
            FROM dashboard_evidence_documents
            WHERE document_id = 'retrieval_document:week:weekly-current-1'
            """
        ).fetchone()
        self.assertEqual("week", evidence["document_type"])
        self.assertIn("Current week", evidence["title"])
        self.assertIn("activity-current-1", evidence["evidence_json"])
        self.assertIn("missing_sleep", evidence["caveats_json"])
        self.assertIsNone(evidence["stale_reason"])

        annotations = self.connection.execute(
            """
            SELECT annotation_type, label, text, linked_record_ids
            FROM dashboard_context_annotations
            ORDER BY annotation_type
            """
        ).fetchall()
        labels = {(row["annotation_type"], row["label"]) for row in annotations}
        self.assertIn(("context_note", "injury"), labels)
        self.assertIn(("race", "Hamilton Marathon"), labels)
        self.assertIn(("training_block", "Current Hamilton build"), labels)

    def test_empty_reporting_views_are_queryable(self) -> None:
        empty = connect_database(Path(self.temporary_directory.name) / "empty.sqlite3")
        try:
            apply_migrations(empty)
            for view in (
                "dashboard_weekly_training",
                "dashboard_metric_timeseries",
                "dashboard_block_comparison",
                "dashboard_evidence_documents",
                "dashboard_context_annotations",
                "dashboard_race_projection_status",
            ):
                rows = empty.execute(f"SELECT * FROM {view}").fetchall()
                self.assertEqual([], rows)
        finally:
            empty.close()

    def _seed_dashboard_data(self) -> None:
        self._record("activity-current-1", "activity", "user_entered")
        self.connection.execute(
            """
            INSERT INTO activities (
                record_id, activity_type, started_at, duration_seconds,
                distance_metres, title
            ) VALUES (
                'activity-current-1', 'run', '2026-11-02T11:00:00+00:00',
                3600, 10000, 'Local dashboard fixture run'
            )
            """
        )
        self._weekly(
            "weekly-prior-1",
            "2026-10-26",
            "2026-11-01",
            distance=30000,
            long_run=16000,
            duration=10800,
            gaps="[]",
        )
        self._weekly(
            "weekly-current-1",
            "2026-11-02",
            "2026-11-08",
            distance=42000,
            long_run=21000,
            duration=15120,
            gaps='["missing_sleep"]',
            caveat="recovery coverage is incomplete",
        )
        self._metric(
            "metric-recovery-current",
            "recovery_coverage",
            0.5,
            "ratio",
            "2026-11-02T05:00:00+00:00",
            "2026-11-09T05:00:00+00:00",
            quality=0.5,
            evidence_id="activity-current-1",
            caveat="recovery coverage is incomplete",
        )
        self._metric(
            "metric-load-current",
            "training_load_trend",
            1.4,
            "ratio",
            "2026-11-02T05:00:00+00:00",
            "2026-11-09T05:00:00+00:00",
            quality=1.0,
            evidence_id="activity-current-1",
        )
        self._race("race-current", "Hamilton Marathon")
        self._block(
            "block-prior",
            "Prior base block",
            "2026-10-26",
            "2026-11-01",
            None,
        )
        self._block(
            "block-current",
            "Current Hamilton build",
            "2026-11-02",
            "2026-11-08",
            "race-current",
        )
        self._note("note-injury", "injury", "Left calf tight", "activity-current-1")
        self.connection.execute(
            """
            INSERT INTO retrieval_documents (
                document_id, document_type, source_record_id, source_updated_at,
                title, body, metadata_json, evidence_json, caveats_json,
                document_version, generated_at, stale_reason
            ) VALUES (
                'retrieval_document:week:weekly-current-1', 'week',
                'weekly-current-1', '2026-11-09T12:00:00+00:00',
                'Current week evidence', 'Current week local evidence',
                '{}', '["activity-current-1"]', '["missing_sleep"]',
                '1.0.0', '2026-11-09T13:00:00+00:00', NULL
            )
            """
        )

    def _record(
        self,
        record_id: str,
        record_type: str,
        provenance_kind: str,
        *,
        method_name: str | None = None,
        method_version: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind, method_name, method_version
            ) VALUES (?, ?, 'America/Toronto',
                      '2026-11-02T12:00:00+00:00',
                      '2026-11-09T12:00:00+00:00',
                      ?, ?, ?)
            """,
            (record_id, record_type, provenance_kind, method_name, method_version),
        )

    def _weekly(
        self,
        record_id: str,
        week_start: str,
        week_end: str,
        *,
        distance: float,
        long_run: float,
        duration: float,
        gaps: str,
        caveat: str | None = None,
    ) -> None:
        self._record(
            record_id,
            "weekly_summary",
            "computed",
            method_name="weekly_summary",
            method_version="1.0.0",
        )
        self.connection.execute(
            """
            INSERT INTO weekly_summaries (
                record_id, week_start_date, week_end_date, timezone, run_count,
                distance_metres, duration_seconds, long_run_metres,
                average_pace_seconds_per_kilometre, data_gaps_json
            ) VALUES (?, ?, ?, 'America/Toronto', 4, ?, ?, ?, 360, ?)
            """,
            (record_id, week_start, week_end, distance, duration, long_run, gaps),
        )
        if caveat is not None:
            self.connection.execute(
                """
                INSERT INTO provenance_caveats (record_id, position, caveat)
                VALUES (?, 0, ?)
                """,
                (record_id, caveat),
            )

    def _metric(
        self,
        record_id: str,
        key: str,
        value: float,
        unit: str,
        window_start: str,
        window_end: str,
        *,
        quality: float,
        evidence_id: str,
        caveat: str | None = None,
    ) -> None:
        self._record(
            record_id,
            "metric_evidence",
            "computed",
            method_name=key,
            method_version="1.0.0",
        )
        self.connection.execute(
            """
            INSERT INTO metric_evidence (
                record_id, metric_key, metric_value, unit, quality,
                window_start, window_end, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (record_id, key, value, unit, quality, window_start, window_end, key),
        )
        self.connection.execute(
            """
            INSERT INTO provenance_evidence (
                record_id, evidence_record_id, position
            ) VALUES (?, ?, 0)
            """,
            (record_id, evidence_id),
        )
        if caveat is not None:
            self.connection.execute(
                """
                INSERT INTO provenance_caveats (record_id, position, caveat)
                VALUES (?, 0, ?)
                """,
                (record_id, caveat),
            )

    def _race(self, record_id: str, name: str) -> None:
        self._record(record_id, "race", "user_entered")
        self.connection.execute(
            """
            INSERT INTO races (
                record_id, name, started_at, distance_metres,
                result_duration_seconds, target_duration_seconds
            ) VALUES (?, ?, '2026-11-08T12:00:00+00:00', 42195, NULL, 11400)
            """,
            (record_id, name),
        )

    def _block(
        self,
        record_id: str,
        name: str,
        start: str,
        end: str,
        target_race_id: str | None,
    ) -> None:
        self._record(record_id, "training_block", "user_entered")
        self.connection.execute(
            """
            INSERT INTO training_blocks (
                record_id, name, start_date, end_date, target_race_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (record_id, name, start, end, target_race_id),
        )

    def _note(
        self,
        record_id: str,
        kind: str,
        text: str,
        linked_record_id: str,
    ) -> None:
        self._record(record_id, "context_note", "user_entered")
        self.connection.execute(
            """
            INSERT INTO context_notes (
                record_id, occurred_at, note_kind, note_text
            ) VALUES (?, '2026-11-03T12:00:00+00:00', ?, ?)
            """,
            (record_id, kind, text),
        )
        self.connection.execute(
            """
            INSERT INTO context_note_links (note_id, linked_record_id)
            VALUES (?, ?)
            """,
            (record_id, linked_record_id),
        )


class GrafanaDashboardDefinitionTests(unittest.TestCase):
    def test_dashboard_json_uses_stable_local_sqlite_datasource(self) -> None:
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

        self.assertEqual("trainingos-local-dashboard", dashboard["uid"])
        self.assertEqual("TrainingOS Local Dashboard", dashboard["title"])
        panel_ids = [panel["id"] for panel in dashboard["panels"]]
        self.assertEqual(len(panel_ids), len(set(panel_ids)))
        self.assertGreaterEqual(len(panel_ids), 10)

        queries = _dashboard_queries(dashboard)
        self.assertTrue(queries)
        for query in queries:
            self.assertIn("dashboard_", query)
            self.assertNotIn("raw_source_records", query)
            self.assertNotIn("garmin", query.lower())
            self.assertNotIn("strava", query.lower())
            self.assertNotIn("openai", query.lower())
            self.assertNotIn("anthropic", query.lower())
            self.assertNotIn("ollama", query.lower())

        datasource_uids = _datasource_uids(dashboard)
        self.assertEqual({"trainingos-sqlite"}, datasource_uids)

    def test_dashboard_has_required_panels_units_and_annotations(self) -> None:
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        panels = {panel["title"]: panel for panel in dashboard["panels"]}

        for title in (
            "Weekly Distance",
            "Training Load Trend",
            "Long Run Progression",
            "Marathon-Pace Volume",
            "Aerobic Efficiency",
            "Heart-Rate Drift",
            "Recovery Coverage",
            "Current vs Prior Block",
            "Race Projection Evidence",
            "Missing Data and Caveats",
            "Evidence Documents",
        ):
            self.assertIn(title, panels)

        self.assertEqual("km", _unit(panels["Weekly Distance"]))
        self.assertEqual("km", _unit(panels["Long Run Progression"]))
        self.assertEqual("m", _unit(panels["Marathon-Pace Volume"]))
        self.assertEqual("percent", _unit(panels["Heart-Rate Drift"]))
        self.assertEqual("percentunit", _unit(panels["Recovery Coverage"]))

        annotations = dashboard["annotations"]["list"]
        self.assertEqual(1, len(annotations))
        self.assertIn("dashboard_context_annotations", json.dumps(annotations))

    def test_provisioning_files_are_local_and_reproducible(self) -> None:
        datasource = DATASOURCE_PATH.read_text(encoding="utf-8")
        compose = COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertIn("uid: trainingos-sqlite", datasource)
        self.assertIn("type: frser-sqlite-datasource", datasource)
        self.assertIn("path: /var/lib/trainingos/trainingos.sqlite3", datasource)
        self.assertIn("GF_INSTALL_PLUGINS: frser-sqlite-datasource", compose)
        self.assertIn("/var/lib/trainingos/trainingos.sqlite3:ro", compose)
        self.assertNotIn("password:", datasource.lower())


def _dashboard_queries(dashboard: dict[str, Any]) -> tuple[str, ...]:
    queries: list[str] = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            raw_sql = target.get("rawSql")
            if raw_sql:
                queries.append(raw_sql)
    for annotation in dashboard["annotations"]["list"]:
        raw_sql = annotation.get("rawSql")
        if raw_sql:
            queries.append(raw_sql)
    return tuple(queries)


def _datasource_uids(dashboard: dict[str, Any]) -> set[str]:
    uids = set()
    for panel in dashboard["panels"]:
        datasource = panel.get("datasource")
        if datasource:
            uids.add(datasource["uid"])
        for target in panel.get("targets", []):
            datasource = target.get("datasource")
            if datasource:
                uids.add(datasource["uid"])
    for annotation in dashboard["annotations"]["list"]:
        datasource = annotation.get("datasource")
        if datasource:
            uids.add(datasource["uid"])
    return uids


def _unit(panel: dict[str, Any]) -> str:
    return panel["fieldConfig"]["defaults"]["unit"]


if __name__ == "__main__":
    unittest.main()
