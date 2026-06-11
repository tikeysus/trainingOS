from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from trainingos.storage import (
    Migration,
    MigrationError,
    apply_migrations,
    connect_database,
    discover_migrations,
)


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

    def test_clean_database_applies_ordered_schema(self) -> None:
        applied = apply_migrations(self.connection)

        self.assertEqual([1], [migration.version for migration in applied])
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
                "raw_source_records",
                "records",
                "source_references",
                "activities",
                "laps",
                "activity_samples",
                "daily_health",
                "context_notes",
                "weather_observations",
                "races",
                "training_blocks",
                "metric_evidence",
                "metric_values",
                "provenance_evidence",
                "provenance_caveats",
            }.issubset(tables)
        )

    def test_reapplying_migrations_is_idempotent(self) -> None:
        first = apply_migrations(self.connection)
        second = apply_migrations(self.connection)

        self.assertEqual(first, second)
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
        )

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
            apply_migrations(self.connection, (changed,))

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


if __name__ == "__main__":
    unittest.main()
