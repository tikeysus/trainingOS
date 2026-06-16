from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trainingos.config import AppConfig
from trainingos.ingestion import (
    FitMessage,
    ManualFitAdapter,
    ManualFitHandler,
    RawArtifactStore,
    SyncRunner,
    SyncStatus,
    parse_fit_messages,
)
from trainingos.ingestion import fit_import
from trainingos.storage import apply_migrations, connect_database


class FitIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "training.sqlite3"
        self.raw_dir = self.root / "raw"
        self.connection = connect_database(self.database_path)
        apply_migrations(self.connection)
        self.clock = lambda: datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_parse_fit_messages_maps_activity_laps_and_samples(self) -> None:
        parsed = parse_fit_messages(
            self._messages(),
            raw_record_id="raw-1",
            source="manual_fit",
            fallback_external_id="sha256:fallback",
            synced_at=self.clock(),
            timezone="America/Toronto",
        )

        self.assertEqual(
            "fit:12345:2026-06-16T10:00:00+00:00",
            parsed.external_id,
        )
        self.assertEqual("run", parsed.activity.activity_type.value)
        self.assertEqual(3600.0, parsed.activity.duration.value)
        self.assertEqual(10000.0, parsed.activity.distance.value)
        self.assertEqual(2, len(parsed.laps))
        self.assertEqual(2, len(parsed.samples))
        self.assertEqual("America/Toronto", parsed.activity.metadata.timezone)
        self.assertEqual(
            ("fitdecode_fit_parser", "1.0.0"),
            (
                parsed.activity.metadata.source_references[0].parser.name,
                parsed.activity.metadata.source_references[0].parser.version,
            ),
        )
        sample_metrics = {
            metric.key: (metric.measurement.value, metric.measurement.unit.value)
            for metric in parsed.samples[0].metrics
        }
        self.assertEqual((150, "bpm"), sample_metrics["heart_rate"])
        self.assertEqual((3.2, "m/s"), sample_metrics["speed"])
        self.assertEqual((250, "W"), sample_metrics["power"])

    def test_manual_fit_import_retains_raw_file_and_normalizes_idempotently(
        self,
    ) -> None:
        fit_path = self.root / "sanitized.fit"
        fit_path.write_bytes(b"sanitized fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=self._messages(),
        ):
            first = runner.run(ManualFitAdapter((fit_path,)), handler)
            second = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, first.status)
        self.assertEqual(1, first.imported_count)
        self.assertEqual(SyncStatus.COMPLETED, second.status)
        self.assertEqual(0, second.imported_count)
        self.assertEqual(1, second.skipped_count)
        self.assertEqual(1, self._count("raw_source_records"))
        self.assertEqual(1, self._count("activities"))
        self.assertEqual(2, self._count("laps"))
        self.assertEqual(2, self._count("activity_samples"))
        raw = self.connection.execute(
            """
            SELECT checksum, storage_path, payload
            FROM raw_source_records
            WHERE source = 'manual_fit'
            """
        ).fetchone()
        self.assertTrue(raw["checksum"].startswith("sha256:"))
        self.assertIsNone(raw["payload"])
        self.assertEqual(b"sanitized fit bytes", Path(raw["storage_path"]).read_bytes())
        self.assertTrue(str(raw["storage_path"]).startswith(str(self.raw_dir)))

    def test_malformed_fit_fails_without_normalized_or_raw_rows(self) -> None:
        fit_path = self.root / "malformed.fit"
        fit_path.write_bytes(b"not a valid sanitized fit")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="UTC",
            clock=self.clock,
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=()):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual(1, report.failed_count)
        self.assertEqual(0, self._count("raw_source_records"))
        self.assertEqual(0, self._count("activities"))
        error = self.connection.execute(
            """
            SELECT error_code, message
            FROM sync_errors
            WHERE sync_run_id = ?
            """,
            (report.sync_run_id,),
        ).fetchone()
        self.assertEqual("fit_session_missing", error["error_code"])
        self.assertNotIn("not a valid", error["message"])

    def test_raw_artifact_store_deduplicates_content_addressed_files(self) -> None:
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at
            ) VALUES (
                'run-1', 'manual_fit', 'running',
                '2026-06-16T12:00:00+00:00'
            )
            """
        )
        store = RawArtifactStore(self.raw_dir)
        first = store.retain_bytes(
            self.connection,
            sync_run_id="run-1",
            source="manual_fit",
            external_id="activity-1",
            record_kind="activity_file",
            content_type="application/vnd.ant.fit",
            content=b"same bytes",
            source_updated_at=self.clock(),
            ingested_at=self.clock(),
        )
        second = store.retain_bytes(
            self.connection,
            sync_run_id="run-1",
            source="manual_fit",
            external_id="activity-1",
            record_kind="activity_file",
            content_type="application/vnd.ant.fit",
            content=b"same bytes",
            source_updated_at=self.clock(),
            ingested_at=self.clock(),
        )

        self.assertEqual(first, second)
        self.assertEqual(1, self._count("raw_source_records"))
        self.assertTrue(first.storage_path.exists())

    def test_fit_import_main_wires_config_migrations_and_sync_runner(self) -> None:
        database_path = self.root / "cli.sqlite3"
        raw_data_dir = self.root / "cli-raw"
        connection = unittest.mock.Mock()
        runner = unittest.mock.Mock()
        runner.run.return_value = SimpleNamespace(status=SyncStatus.COMPLETED)

        with (
            patch.object(
                fit_import.AppConfig,
                "from_env",
                return_value=AppConfig(
                    database_path=database_path,
                    raw_data_dir=raw_data_dir,
                    local_timezone="America/Toronto",
                ),
            ) as from_env,
            patch.object(
                fit_import,
                "connect_database",
                return_value=connection,
            ) as connect_database_mock,
            patch.object(fit_import, "apply_migrations") as apply_migrations_mock,
            patch.object(fit_import, "ManualFitAdapter") as adapter_class,
            patch.object(fit_import, "RawArtifactStore") as raw_store_class,
            patch.object(fit_import, "ManualFitHandler") as handler_class,
            patch.object(
                fit_import,
                "SyncRunner",
                return_value=runner,
            ) as runner_class,
        ):
            exit_code = fit_import.main(["--timezone", "UTC", "workout.fit"])

        self.assertEqual(0, exit_code)
        from_env.assert_called_once_with()
        connect_database_mock.assert_called_once_with(database_path)
        apply_migrations_mock.assert_called_once_with(connection)
        adapter_class.assert_called_once_with([Path("workout.fit")])
        raw_store_class.assert_called_once_with(raw_data_dir)
        handler_class.assert_called_once_with(
            raw_store_class.return_value,
            timezone="UTC",
        )
        runner_class.assert_called_once_with(connection)
        runner.run.assert_called_once_with(
            adapter_class.return_value,
            handler_class.return_value,
        )
        connection.close.assert_called_once_with()

    def test_fit_import_main_uses_config_timezone_and_returns_failure(self) -> None:
        connection = unittest.mock.Mock()
        runner = unittest.mock.Mock()
        runner.run.return_value = SimpleNamespace(status=SyncStatus.FAILED)

        with (
            patch.object(
                fit_import.AppConfig,
                "from_env",
                return_value=AppConfig(
                    database_path=self.root / "cli.sqlite3",
                    raw_data_dir=self.root / "cli-raw",
                    local_timezone="America/Toronto",
                ),
            ),
            patch.object(fit_import, "connect_database", return_value=connection),
            patch.object(fit_import, "apply_migrations"),
            patch.object(fit_import, "ManualFitAdapter"),
            patch.object(fit_import, "RawArtifactStore") as raw_store_class,
            patch.object(fit_import, "ManualFitHandler") as handler_class,
            patch.object(fit_import, "SyncRunner", return_value=runner),
        ):
            exit_code = fit_import.main(["workout.fit"])

        self.assertEqual(1, exit_code)
        handler_class.assert_called_once_with(
            raw_store_class.return_value,
            timezone="America/Toronto",
        )
        connection.close.assert_called_once_with()

    def _messages(self) -> tuple[FitMessage, ...]:
        return (
            FitMessage(
                "file_id",
                {
                    "serial_number": 12345,
                    "time_created": datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
                },
            ),
            FitMessage(
                "session",
                {
                    "sport": "running",
                    "start_time": datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
                    "timestamp": datetime(2026, 6, 16, 11, 2, tzinfo=UTC),
                    "total_timer_time": 3600,
                    "total_distance": 10000,
                },
            ),
            FitMessage(
                "lap",
                {
                    "start_time": datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
                    "timestamp": datetime(2026, 6, 16, 10, 30, tzinfo=UTC),
                    "total_timer_time": 1800,
                    "total_distance": 5000,
                },
            ),
            FitMessage(
                "lap",
                {
                    "start_time": datetime(2026, 6, 16, 10, 30, tzinfo=UTC),
                    "timestamp": datetime(2026, 6, 16, 11, 0, tzinfo=UTC),
                    "total_timer_time": 1800,
                    "total_distance": 5000,
                },
            ),
            FitMessage(
                "record",
                {
                    "timestamp": datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
                    "heart_rate": 150,
                    "enhanced_speed": 3.2,
                    "power": 250,
                },
            ),
            FitMessage(
                "record",
                {
                    "timestamp": datetime(2026, 6, 16, 10, 1, tzinfo=UTC),
                    "heart_rate": 151,
                    "cadence": 88,
                    "temperature": 21,
                },
            ),
        )

    def _count(self, table: str) -> int:
        if table not in {
            "raw_source_records",
            "activities",
            "laps",
            "activity_samples",
        }:
            raise ValueError("unsupported table")
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
