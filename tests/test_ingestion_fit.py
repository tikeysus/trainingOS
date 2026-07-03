from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trainingos.config import DATABASE_PATH_ENV, AppConfig
from trainingos.ingestion import (
    FitMessage,
    ManualFitAdapter,
    ManualFitHandler,
    RawArtifactStore,
    SyncOptions,
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
        self.assertEqual(
            parsed.laps[1].started_at,
            parsed.laps[1].metadata.updated_at,
        )
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

    def test_garmin_export_zip_discovers_nested_fit_files_without_pii(self) -> None:
        outer_zip = self.root / "garmin-export.zip"
        nested_bytes = BytesIO()
        with zipfile.ZipFile(nested_bytes, "w") as nested:
            nested.writestr("runner@example.com_123.fit", b"activity fit")
            nested.writestr("runner@example.com_profile.json", b'{"ignored": true}')
        with zipfile.ZipFile(outer_zip, "w") as outer:
            outer.writestr("customer_data/customer.json", b'{"ignored": true}')
            outer.writestr(
                "DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip",
                nested_bytes.getvalue(),
            )

        adapter = ManualFitAdapter((outer_zip,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))
        record = page.records[0]
        self.assertTrue(record.external_id.startswith("zip-fit:1:1:sha256:"))
        self.assertNotIn("runner@example.com", record.external_id)
        self.assertEqual(b"activity fit", record.payload.path.read_bytes())
        self.assertTrue(record.payload.skip_missing_session)

    def test_garmin_export_zip_skips_non_activity_fit_and_imports_activity(
        self,
    ) -> None:
        export_zip = self.root / "garmin-export.zip"
        with zipfile.ZipFile(export_zip, "w") as archive:
            archive.writestr("a-device-settings.fit", b"settings fit")
            archive.writestr("b-activity.fit", b"activity fit")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            side_effect=((), self._messages()),
        ):
            report = runner.run(ManualFitAdapter((export_zip,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.imported_count)
        self.assertEqual(1, report.skipped_count)
        self.assertEqual(1, self._count("raw_source_records"))
        self.assertEqual(1, self._count("activities"))
        raw = self.connection.execute(
            "SELECT storage_path FROM raw_source_records WHERE source = 'manual_fit'"
        ).fetchone()
        self.assertEqual(b"activity fit", Path(raw["storage_path"]).read_bytes())

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
        fit_path = self.root / "workout.fit"
        fit_path.write_bytes(b"fit")
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
            patch.object(fit_import, "refresh_training_data") as refresh_mock,
            patch.object(
                fit_import,
                "SyncRunner",
                return_value=runner,
            ) as runner_class,
        ):
            exit_code = fit_import.main(["--timezone", "UTC", str(fit_path)])

        self.assertEqual(0, exit_code)
        from_env.assert_called_once_with()
        connect_database_mock.assert_called_once_with(database_path)
        apply_migrations_mock.assert_called_once_with(connection)
        adapter_class.assert_called_once_with([fit_path])
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
        refresh_mock.assert_called_once_with(connection, timezone="UTC")
        connection.close.assert_called_once_with()

    def test_fit_import_main_uses_config_timezone_and_returns_failure(self) -> None:
        fit_path = self.root / "workout.fit"
        fit_path.write_bytes(b"fit")
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
            patch.object(fit_import, "refresh_training_data") as refresh_mock,
            patch.object(fit_import, "SyncRunner", return_value=runner),
        ):
            exit_code = fit_import.main([str(fit_path)])

        self.assertEqual(1, exit_code)
        handler_class.assert_called_once_with(
            raw_store_class.return_value,
            timezone="America/Toronto",
        )
        refresh_mock.assert_not_called()
        connection.close.assert_called_once_with()

    def test_fit_import_cli_rejects_missing_path_without_traceback(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "trainingos.ingestion.fit_import",
                "/path/to/file-or-directory-or-export.zip",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=self._pythonpath_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("import path does not exist", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_fit_import_cli_rejects_unreadable_zip_without_traceback(self) -> None:
        bad_zip = self.root / "bad.zip"
        bad_zip.write_bytes(b"not really a zip")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "trainingos.ingestion.fit_import",
                str(bad_zip),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=self._pythonpath_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("FIT zip archive could not be read", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

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
                    "timestamp": datetime(2026, 6, 16, 10, 29, tzinfo=UTC),
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

    def _pythonpath_env(self) -> dict[str, str]:
        env = os.environ.copy()
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{src_path}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else src_path
        )
        env[DATABASE_PATH_ENV] = str(self.database_path)
        return env


class FitImportIdentityTests(unittest.TestCase):
    """Tests for stable, privacy-preserving FIT import identity (issue #24)."""

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

    # ── A. Happy Path ────────────────────────────────────────────────────────

    def test_plain_fit_adapter_external_id_does_not_contain_local_path(self) -> None:
        fit_path = self.root / "my_workout.fit"
        fit_path.write_bytes(b"valid fit bytes for run on 2026-06-16")

        page = ManualFitAdapter((fit_path,)).fetch(None, 100)

        self.assertEqual(1, len(page.records))
        record = page.records[0]
        self.assertNotIn(str(self.root), record.external_id)
        self.assertNotIn("my_workout", record.external_id)

    def test_plain_fit_adapter_external_id_is_content_hash(self) -> None:
        content = b"deterministic fit bytes for identity test"
        fit_path = self.root / "workout.fit"
        fit_path.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()

        page = ManualFitAdapter((fit_path,)).fetch(None, 100)

        self.assertIn(expected[:16], page.records[0].external_id)

    def test_directory_adapter_external_ids_do_not_contain_absolute_paths(self) -> None:
        fit_dir = self.root / "garmin_exports"
        fit_dir.mkdir()
        (fit_dir / "morning_run.fit").write_bytes(b"fit bytes one for morning")
        (fit_dir / "evening_run.fit").write_bytes(b"fit bytes two for evening")

        page = ManualFitAdapter((fit_dir,)).fetch(None, 100)

        self.assertEqual(2, len(page.records))
        for record in page.records:
            self.assertNotIn(str(self.root), record.external_id)
            self.assertNotIn("garmin_exports", record.external_id)
            self.assertNotIn("morning_run", record.external_id)
            self.assertNotIn("evening_run", record.external_id)

    def test_full_import_does_not_leak_path_into_source_references(self) -> None:
        personal_dir = self.root / "Users" / "athlete" / "garmin"
        personal_dir.mkdir(parents=True)
        fit_path = personal_dir / "my_run.fit"
        fit_path.write_bytes(b"personal activity fit data")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="America/Toronto", clock=self.clock
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            runner.run(ManualFitAdapter((fit_path,)), handler)

        refs = self.connection.execute(
            "SELECT external_id FROM source_references WHERE source = 'manual_fit'"
        ).fetchall()
        self.assertGreater(len(refs), 0)
        for row in refs:
            self.assertNotIn(str(self.root), row["external_id"])
            self.assertNotIn("athlete", row["external_id"])
            self.assertNotIn("my_run", row["external_id"])

    # ── B. Boundary Values ───────────────────────────────────────────────────

    def test_fit_identity_with_missing_time_created_falls_back_to_content_hash(self) -> None:
        # file_id has serial but no time_created; session also has no start_time
        content = b"fit bytes no timestamps"
        messages = (
            FitMessage("file_id", {"serial_number": 77777}),
            FitMessage(
                "session",
                {
                    "sport": "running",
                    "timestamp": datetime(2026, 6, 16, 11, 0, tzinfo=UTC),
                    "total_timer_time": 1800,
                    "total_distance": 5000,
                },
            ),
        )

        parsed = parse_fit_messages(
            messages,
            raw_record_id="raw-1",
            source="manual_fit",
            fallback_external_id=f"sha256:{hashlib.sha256(content).hexdigest()}",
            synced_at=self.clock(),
            timezone="UTC",
        )

        self.assertTrue(
            parsed.external_id.startswith("sha256:"),
            f"expected content-hash fallback, got: {parsed.external_id}",
        )
        self.assertNotIn("77777", parsed.external_id)

    def test_fit_identity_uses_session_start_time_when_file_id_lacks_time_created(self) -> None:
        # file_id has serial but no time_created; session provides start_time as fallback
        messages = (
            FitMessage("file_id", {"serial_number": 55555}),
            FitMessage(
                "session",
                {
                    "sport": "running",
                    "start_time": datetime(2026, 6, 16, 8, 0, tzinfo=UTC),
                    "timestamp": datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
                    "total_timer_time": 3600,
                    "total_distance": 10000,
                },
            ),
        )

        parsed = parse_fit_messages(
            messages,
            raw_record_id="raw-1",
            source="manual_fit",
            fallback_external_id="sha256:fallback",
            synced_at=self.clock(),
            timezone="UTC",
        )

        self.assertEqual("fit:55555:2026-06-16T08:00:00+00:00", parsed.external_id)

    def test_directory_with_single_fit_file_produces_one_non_path_record(self) -> None:
        fit_dir = self.root / "solo"
        fit_dir.mkdir()
        (fit_dir / "only.fit").write_bytes(b"solo fit bytes")

        page = ManualFitAdapter((fit_dir,)).fetch(None, 100)

        self.assertEqual(1, len(page.records))
        self.assertNotIn(str(self.root), page.records[0].external_id)

    def test_directory_with_no_fit_files_produces_empty_page(self) -> None:
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        (empty_dir / "notes.txt").write_text("not a fit file")
        (empty_dir / "data.json").write_text("{}")

        page = ManualFitAdapter((empty_dir,)).fetch(None, 100)

        self.assertEqual(0, len(page.records))
        self.assertTrue(page.done)

    # ── C. Equivalence Partitioning ──────────────────────────────────────────

    def test_plain_fit_and_zip_fit_with_identical_content_share_hash(self) -> None:
        # Both paths produce an external_id that encodes the same content hash
        content = b"shared fit bytes no file id message"
        plain_path = self.root / "plain.fit"
        plain_path.write_bytes(content)

        zip_path = self.root / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("member.fit", content)

        plain_record = ManualFitAdapter((plain_path,)).fetch(None, 100).records[0]
        zip_record = ManualFitAdapter((zip_path,)).fetch(None, 100).records[0]

        expected_hash = hashlib.sha256(content).hexdigest()[:16]
        self.assertIn(expected_hash, plain_record.external_id)
        self.assertIn(expected_hash, zip_record.external_id)

    def test_fit_files_with_same_device_identity_produce_same_parsed_external_id(self) -> None:
        # Two parsings of activities with the same serial+time_created are the same identity
        shared_time = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
        base_fields = {
            "sport": "running",
            "start_time": shared_time,
            "timestamp": shared_time,
            "total_timer_time": 3600,
            "total_distance": 10000,
        }
        messages_a = (
            FitMessage("file_id", {"serial_number": 12345, "time_created": shared_time}),
            FitMessage("session", base_fields),
        )
        messages_b = (
            FitMessage("file_id", {"serial_number": 12345, "time_created": shared_time}),
            FitMessage("session", {**base_fields, "total_calories": 500}),
        )

        parsed_a = parse_fit_messages(
            messages_a, raw_record_id="r1", source="manual_fit",
            fallback_external_id="sha256:a", synced_at=self.clock(), timezone="UTC",
        )
        parsed_b = parse_fit_messages(
            messages_b, raw_record_id="r2", source="manual_fit",
            fallback_external_id="sha256:b", synced_at=self.clock(), timezone="UTC",
        )

        self.assertEqual(parsed_a.external_id, parsed_b.external_id)
        self.assertEqual("fit:12345:2026-06-16T10:00:00+00:00", parsed_a.external_id)

    # ── D. Edge Cases ────────────────────────────────────────────────────────

    def test_same_content_in_two_differently_named_files_is_deduplicated(self) -> None:
        content = b"same content different filenames"
        path_a = self.root / "workout_original.fit"
        path_b = self.root / "workout_copy.fit"
        path_a.write_bytes(content)
        path_b.write_bytes(content)

        page = ManualFitAdapter((path_a, path_b)).fetch(None, 100)

        # Same content → same identity → adapter deduplicates before sync
        self.assertEqual(1, len(page.records))

    def test_same_file_moved_to_new_path_is_not_reimported(self) -> None:
        content = b"stable fit content that will move paths"
        path_a = self.root / "dir_a" / "workout.fit"
        path_a.parent.mkdir()
        path_a.write_bytes(content)
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="America/Toronto", clock=self.clock
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            first = runner.run(ManualFitAdapter((path_a,)), handler)

        path_b = self.root / "dir_b" / "renamed_workout.fit"
        path_b.parent.mkdir()
        path_b.write_bytes(content)

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            second = runner.run(ManualFitAdapter((path_b,)), handler)

        self.assertEqual(1, first.imported_count)
        self.assertEqual(SyncStatus.COMPLETED, second.status)
        self.assertEqual(0, second.imported_count)
        self.assertEqual(1, second.skipped_count)
        self.assertEqual(1, self._count("activities"))

    def test_zip_member_ordering_change_produces_same_external_ids(self) -> None:
        content_a = b"activity fit bytes alpha version one"
        content_b = b"activity fit bytes beta version one"

        zip_ordered = self.root / "ordered.zip"
        with zipfile.ZipFile(zip_ordered, "w") as archive:
            archive.writestr("alpha.fit", content_a)
            archive.writestr("beta.fit", content_b)

        zip_reversed = self.root / "reversed.zip"
        with zipfile.ZipFile(zip_reversed, "w") as archive:
            archive.writestr("beta.fit", content_b)
            archive.writestr("alpha.fit", content_a)

        ordered_ids = {r.external_id for r in ManualFitAdapter((zip_ordered,)).fetch(None, 100).records}
        reversed_ids = {r.external_id for r in ManualFitAdapter((zip_reversed,)).fetch(None, 100).records}

        self.assertEqual(ordered_ids, reversed_ids)

    def test_zip_member_filename_containing_pii_does_not_appear_in_external_id(self) -> None:
        zip_path = self.root / "garmin-export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("runner@example.com_20260616.fit", b"activity bytes")

        page = ManualFitAdapter((zip_path,)).fetch(None, 100)

        self.assertEqual(1, len(page.records))
        self.assertNotIn("runner@example.com", page.records[0].external_id)

    def test_checkpoint_cursor_does_not_contain_absolute_path(self) -> None:
        personal_dir = self.root / "Users" / "athlete" / "garmin"
        personal_dir.mkdir(parents=True)
        fit_path = personal_dir / "run.fit"
        fit_path.write_bytes(b"personal run data")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="America/Toronto", clock=self.clock
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            runner.run(ManualFitAdapter((fit_path,)), handler)

        checkpoint = self.connection.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = 'manual_fit'"
        ).fetchone()
        self.assertIsNotNone(checkpoint)
        self.assertNotIn("athlete", checkpoint["cursor"])
        self.assertNotIn("garmin", checkpoint["cursor"])
        self.assertNotIn("run.fit", checkpoint["cursor"])
        self.assertNotIn(str(self.root), checkpoint["cursor"])

    def test_sync_error_external_id_does_not_contain_local_path(self) -> None:
        personal_dir = self.root / "Users" / "athlete" / "garmin"
        personal_dir.mkdir(parents=True)
        fit_path = personal_dir / "malformed.fit"
        fit_path.write_bytes(b"not a valid fit session data")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="UTC", clock=self.clock
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=()):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.FAILED, report.status)
        error = self.connection.execute(
            "SELECT external_id FROM sync_errors WHERE sync_run_id = ?",
            (report.sync_run_id,),
        ).fetchone()
        self.assertIsNotNone(error)
        self.assertNotIn("athlete", error["external_id"] or "")
        self.assertNotIn(str(self.root), error["external_id"] or "")
        self.assertNotIn("malformed.fit", error["external_id"] or "")

    def test_duplicate_paths_in_input_list_are_deduplicated(self) -> None:
        fit_path = self.root / "workout.fit"
        fit_path.write_bytes(b"dedup fit bytes test")

        page = ManualFitAdapter((fit_path, fit_path)).fetch(None, 100)

        self.assertEqual(1, len(page.records))

    # ── E. Error / Negative Tests ────────────────────────────────────────────

    def test_fit_identity_with_none_serial_falls_back_to_content_hash(self) -> None:
        content = b"fit bytes with null serial number"
        messages = (
            FitMessage(
                "file_id",
                {
                    "serial_number": None,
                    "time_created": datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
                },
            ),
            FitMessage(
                "session",
                {
                    "sport": "running",
                    "start_time": datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
                    "timestamp": datetime(2026, 6, 16, 11, 0, tzinfo=UTC),
                    "total_timer_time": 3600,
                    "total_distance": 10000,
                },
            ),
        )

        parsed = parse_fit_messages(
            messages,
            raw_record_id="raw-1",
            source="manual_fit",
            fallback_external_id=f"sha256:{hashlib.sha256(content).hexdigest()}",
            synced_at=self.clock(),
            timezone="UTC",
        )

        self.assertTrue(
            parsed.external_id.startswith("sha256:"),
            f"expected sha256 fallback for None serial, got: {parsed.external_id}",
        )

    def test_zip_member_ids_stable_when_infolist_is_not_alphabetical(self) -> None:
        # Members added in reverse alphabetical order; adapter sorts, so identities are stable
        content_x = b"fit member x bytes unique content"
        content_y = b"fit member y bytes unique content"

        zip_path = self.root / "unordered.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("z_second.fit", content_y)
            archive.writestr("a_first.fit", content_x)

        page = ManualFitAdapter((zip_path,)).fetch(None, 100)

        self.assertEqual(2, len(page.records))
        # Sorted alphabetically → a_first is record 0, z_second is record 1
        self.assertIn(hashlib.sha256(content_x).hexdigest()[:16], page.records[0].external_id)
        self.assertIn(hashlib.sha256(content_y).hexdigest()[:16], page.records[1].external_id)

    # ── F. State-Based / Idempotency Tests ───────────────────────────────────

    def test_repeated_import_with_path_change_skips_already_imported_content(self) -> None:
        content = b"stable bytes that will survive a path rename"
        path_a = self.root / "original.fit"
        path_a.write_bytes(content)
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="UTC", clock=self.clock
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            first = runner.run(ManualFitAdapter((path_a,)), handler)

        path_b = self.root / "subdir" / "moved.fit"
        path_b.parent.mkdir()
        path_b.write_bytes(content)

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            second = runner.run(ManualFitAdapter((path_b,)), handler)

        self.assertEqual(1, first.imported_count)
        self.assertEqual(0, second.imported_count)
        self.assertEqual(1, second.skipped_count)
        self.assertEqual(1, self._count("activities"))

    def test_dry_run_does_not_persist_path_metadata(self) -> None:
        fit_path = self.root / "dry_run_workout.fit"
        fit_path.write_bytes(b"dry run fit data bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="UTC", clock=self.clock
        )

        with patch("trainingos.ingestion.fit.read_fit_messages", return_value=self._messages()):
            report = runner.run(
                ManualFitAdapter((fit_path,)),
                handler,
                SyncOptions(dry_run=True),
            )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(0, self._count("raw_source_records"))
        checkpoint = self.connection.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = 'manual_fit'"
        ).fetchone()
        self.assertIsNone(checkpoint)

    def test_zip_rerun_with_different_member_ordering_does_not_reimport(self) -> None:
        content_a = b"first activity fit bytes content here"
        content_b = b"second activity fit bytes content here"

        zip_a = self.root / "export_alpha_first.zip"
        with zipfile.ZipFile(zip_a, "w") as archive:
            archive.writestr("alpha.fit", content_a)
            archive.writestr("beta.fit", content_b)

        zip_b = self.root / "export_beta_first.zip"
        with zipfile.ZipFile(zip_b, "w") as archive:
            archive.writestr("beta.fit", content_b)
            archive.writestr("alpha.fit", content_a)

        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir), timezone="America/Toronto", clock=self.clock
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            side_effect=((), self._messages()),
        ):
            first = runner.run(ManualFitAdapter((zip_a,)), handler)

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            side_effect=((), self._messages()),
        ):
            second = runner.run(ManualFitAdapter((zip_b,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, first.status)
        self.assertEqual(1, first.imported_count)
        self.assertEqual(SyncStatus.COMPLETED, second.status)
        self.assertEqual(0, second.imported_count)
        self.assertEqual(2, second.skipped_count)
        self.assertEqual(1, self._count("activities"))

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
