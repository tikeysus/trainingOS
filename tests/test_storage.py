from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trainingos.storage import (
    DatabaseConnectionError,
    Migration,
    MigrationError,
    apply_migrations,
    connect_database,
    discover_migrations,
)
from trainingos.storage.migrations import _sql_statements


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        self.connection = connect_database(self.database_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_connection_enables_integrity_and_concurrency_pragmas(self) -> None:
        self.assertEqual(
            1,
            self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
        )
        self.assertEqual(
            "wal",
            self.connection.execute("PRAGMA journal_mode").fetchone()[0],
        )
        self.assertEqual(
            5000,
            self.connection.execute("PRAGMA busy_timeout").fetchone()[0],
        )

    def test_connection_wraps_unusable_database_path(self) -> None:
        with patch("pathlib.Path.mkdir", side_effect=OSError("read-only filesystem")):
            with self.assertRaisesRegex(
                DatabaseConnectionError,
                "cannot open local SQLite database",
            ):
                connect_database(Path("/absolute/path/to/trainingos.sqlite3"))

    def test_clean_database_applies_ordered_schema(self) -> None:
        applied = apply_migrations(self.connection)

        self.assertEqual(
            [1, 2, 3, 4, 5, 6, 7],
            [migration.version for migration in applied],
        )
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "schema_migrations",
                "sync_runs",
                "sync_errors",
                "sync_checkpoints",
                "raw_source_records",
                "records",
                "source_references",
                "activities",
                "laps",
                "activity_samples",
                "daily_health",
                "context_notes",
                "activity_weather_observations",
                "weather_observations",
                "races",
                "training_blocks",
                "metric_evidence",
                "metric_values",
                "weekly_summaries",
                "provenance_evidence",
                "provenance_caveats",
                "retrieval_documents",
                "retrieval_document_fts",
            }.issubset(tables)
        )
        views = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            )
        }
        self.assertTrue(
            {
                "dashboard_weekly_training",
                "dashboard_metric_timeseries",
                "dashboard_block_comparison",
                "dashboard_evidence_documents",
                "dashboard_context_annotations",
                "dashboard_race_projection_status",
            }.issubset(views)
        )

    def test_reapplying_migrations_is_idempotent(self) -> None:
        first = apply_migrations(self.connection)
        second = apply_migrations(self.connection)

        self.assertEqual(first, second)
        self.assertEqual(
            7,
            self.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
        )

    def test_metric_status_migration_preserves_existing_measured_values(
        self,
    ) -> None:
        migrations = discover_migrations()
        apply_migrations(self.connection, migrations[:2])
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at
            ) VALUES (
                'health-1', 'daily_health', 'America/Toronto',
                '2026-06-11T10:00:00+00:00',
                '2026-06-11T10:00:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO metric_values (
                owner_record_id, metric_key, metric_value, unit, quality
            ) VALUES ('health-1', 'resting_heart_rate', 48, 'bpm', 0.9)
            """
        )
        self.connection.commit()

        apply_migrations(self.connection, migrations)

        row = self.connection.execute(
            """
            SELECT metric_status, metric_value, unit, quality
            FROM metric_values
            WHERE owner_record_id = 'health-1'
            """
        ).fetchone()
        self.assertEqual(("measured", 48.0, "bpm", 0.9), tuple(row))

    def test_statement_parser_splits_multiple_statements_on_one_line(self) -> None:
        statements = _sql_statements(
            "CREATE TABLE first (id INTEGER); CREATE TABLE second (id INTEGER);"
        )

        self.assertEqual(
            (
                "CREATE TABLE first (id INTEGER);",
                "CREATE TABLE second (id INTEGER);",
            ),
            statements,
        )

    def test_statement_parser_preserves_semicolons_inside_quoted_text(self) -> None:
        statements = _sql_statements(
            "CREATE TABLE notes (text TEXT); "
            "INSERT INTO notes VALUES ('steady; controlled');"
        )

        self.assertEqual(2, len(statements))
        self.assertEqual(
            "INSERT INTO notes VALUES ('steady; controlled');",
            statements[1],
        )

    def test_statement_parser_supports_multiline_statements(self) -> None:
        statements = _sql_statements(
            "CREATE TABLE activities (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    title TEXT\n"
            ");\n"
        )

        self.assertEqual(1, len(statements))
        self.assertIn("title TEXT", statements[0])

    def test_statement_parser_rejects_incomplete_sql(self) -> None:
        with self.assertRaisesRegex(MigrationError, "incomplete SQL"):
            _sql_statements("CREATE TABLE incomplete (id INTEGER)")

    def test_schema_avoids_redundant_unique_indexes(self) -> None:
        apply_migrations(self.connection)

        source_reference_indexes = self._indexed_columns("source_references")
        daily_health_indexes = self._indexed_columns("daily_health")

        self.assertEqual(
            1,
            source_reference_indexes.count(("record_id", "source", "external_id")),
        )
        self.assertNotIn(
            ("source", "external_id", "record_id"),
            source_reference_indexes,
        )
        self.assertNotIn(("local_date", "record_id"), daily_health_indexes)
        self.assertIn(("local_date",), daily_health_indexes)

    def test_raw_source_identity_is_deduplicated(self) -> None:
        apply_migrations(self.connection)
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES ('run-1', 'fixture', 'completed', '2026-06-11T10:00:00+00:00',
                      '2026-06-11T10:01:00+00:00')
            """
        )
        raw_record = (
            "raw-1",
            "run-1",
            "fixture",
            "activity-42",
            "activity",
            "application/json",
            "sha256:abc",
            b"{}",
            "2026-06-11T10:00:00+00:00",
        )
        self.connection.execute(
            """
            INSERT INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, payload, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            raw_record,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO raw_source_records (
                    raw_record_id, sync_run_id, source, external_id, record_kind,
                    content_type, checksum, payload, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("raw-2", *raw_record[1:]),
            )

    def test_foreign_keys_protect_normalized_records(self) -> None:
        apply_migrations(self.connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO activities (
                    record_id, activity_type, started_at, duration_seconds
                ) VALUES ('missing', 'run', '2026-06-11T10:00:00+00:00', 3600)
                """
            )

    def test_changed_applied_migration_is_rejected(self) -> None:
        migrations = discover_migrations()
        apply_migrations(self.connection, migrations)
        changed = Migration(
            version=1,
            name=migrations[0].name,
            sql=migrations[0].sql,
            checksum="different",
        )

        with self.assertRaisesRegex(MigrationError, "does not match"):
            apply_migrations(self.connection, (changed, *migrations[1:]))

    def test_failed_migration_rolls_back_its_schema_changes(self) -> None:
        broken = Migration(
            version=1,
            name="broken",
            sql=(
                "CREATE TABLE should_rollback (id INTEGER PRIMARY KEY);\n"
                "CREATE TABLE invalid SQL;\n"
            ),
            checksum="fixture",
        )

        with self.assertRaisesRegex(MigrationError, "migration 1_broken failed"):
            apply_migrations(self.connection, (broken,))

        self.assertIsNone(
            self.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'should_rollback'
                """
            ).fetchone()
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
        )

    def _indexed_columns(self, table: str) -> list[tuple[str, ...]]:
        indexes = self.connection.execute(f"PRAGMA index_list({table})").fetchall()
        return [
            tuple(
                row["name"]
                for row in self.connection.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()
            )
            for index in indexes
        ]


if __name__ == "__main__":
    unittest.main()
