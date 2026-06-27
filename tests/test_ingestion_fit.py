from __future__ import annotations

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
    SyncError,
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


class ZipDiscoveryHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    # --- nesting depth ---

    def test_zip_at_max_nesting_depth_discovers_fit_files(self) -> None:
        archive_path = self.root / "depth2.zip"
        inner_bytes = BytesIO()
        with zipfile.ZipFile(inner_bytes, "w") as inner:
            inner.writestr("activity.fit", b"fit data")
        outer_bytes = BytesIO()
        with zipfile.ZipFile(outer_bytes, "w") as outer:
            outer.writestr("nested.zip", inner_bytes.getvalue())
        archive_path.write_bytes(outer_bytes.getvalue())

        adapter = ManualFitAdapter((archive_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))

    def test_zip_exceeding_max_nesting_depth_raises_sync_error(self) -> None:
        archive_path = self.root / "depth3.zip"
        innermost_bytes = BytesIO()
        with zipfile.ZipFile(innermost_bytes, "w") as innermost:
            innermost.writestr("activity.fit", b"fit data")
        middle_bytes = BytesIO()
        with zipfile.ZipFile(middle_bytes, "w") as middle:
            middle.writestr("inner.zip", innermost_bytes.getvalue())
        outer_bytes = BytesIO()
        with zipfile.ZipFile(outer_bytes, "w") as outer:
            outer.writestr("middle.zip", middle_bytes.getvalue())
        archive_path.write_bytes(outer_bytes.getvalue())

        with self.assertRaises(SyncError) as cm:
            ManualFitAdapter((archive_path,))

        self.assertEqual("fit_zip_depth_exceeded", cm.exception.code)

    # --- member count ---

    def test_zip_at_max_member_count_does_not_raise(self) -> None:
        archive_path = self.root / "max_members.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for i in range(1000):
                archive.writestr(f"file_{i:04d}.dat", b"x")

        ManualFitAdapter((archive_path,))

    def test_zip_exceeding_max_member_count_raises_sync_error_without_filename(
        self,
    ) -> None:
        archive_path = self.root / "overflow.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for i in range(1001):
                archive.writestr(f"file_{i:04d}.dat", b"x")

        with self.assertRaises(SyncError) as cm:
            ManualFitAdapter((archive_path,))

        self.assertEqual("fit_zip_member_count_exceeded", cm.exception.code)
        self.assertNotIn("file_", str(cm.exception))

    # --- per-member size ---

    def test_zip_fit_member_at_size_limit_succeeds(self) -> None:
        zip_path = self.root / "at-limit.zip"
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("activity.fit")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"\x00" * 1024)
        zip_path.write_bytes(buf.getvalue())
        with patch("trainingos.ingestion.fit.MAX_ZIP_MEMBER_BYTES", 1024):
            adapter = ManualFitAdapter((zip_path,))
        self.assertEqual(1, len(adapter.fetch(None, 100).records))

    def test_zip_fit_member_exceeds_size_limit_raises_sync_error(self) -> None:
        filename = "oversized.fit"
        zip_path = self.root / "over-limit.zip"
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo(filename)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"\x00" * 1025)
        zip_path.write_bytes(buf.getvalue())
        with patch("trainingos.ingestion.fit.MAX_ZIP_MEMBER_BYTES", 1024):
            with self.assertRaises(SyncError) as cm:
                ManualFitAdapter((zip_path,))
        self.assertEqual("fit_zip_member_too_large", cm.exception.code)
        self.assertNotIn(filename, str(cm.exception))
        self.assertNotIn("1025", str(cm.exception))

    def test_zip_nested_member_exceeds_size_limit_raises_sync_error(self) -> None:
        zip_path = self.root / "nested-over-limit.zip"
        outer_buf = BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as outer:
            nested_info = zipfile.ZipInfo("nested.zip")
            nested_info.compress_type = zipfile.ZIP_STORED
            outer.writestr(nested_info, b"\x00" * 1025)
        zip_path.write_bytes(outer_buf.getvalue())
        with patch("trainingos.ingestion.fit.MAX_ZIP_MEMBER_BYTES", 1024):
            with self.assertRaises(SyncError) as cm:
                ManualFitAdapter((zip_path,))
        self.assertEqual("fit_zip_member_too_large", cm.exception.code)
        self.assertNotIn("nested.zip", str(cm.exception))
        self.assertNotIn("1025", str(cm.exception))

    # --- total uncompressed size ---

    def test_zip_fit_members_within_individual_limit_but_total_exceeded_raises_sync_error(self) -> None:
        zip_path = self.root / "total-exceeded.zip"
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(3):
                info = zipfile.ZipInfo(f"activity-{i}.fit")
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, b"\x00" * 800)
        zip_path.write_bytes(buf.getvalue())
        with (
            patch("trainingos.ingestion.fit.MAX_ZIP_MEMBER_BYTES", 4096),
            patch("trainingos.ingestion.fit.MAX_ZIP_TOTAL_BYTES", 2000),
        ):
            with self.assertRaises(SyncError) as cm:
                ManualFitAdapter((zip_path,))
        self.assertEqual("fit_zip_total_size_exceeded", cm.exception.code)

    def test_zip_nested_fit_members_cumulative_total_exceeded_raises_sync_error(self) -> None:
        nested_buf = BytesIO()
        with zipfile.ZipFile(nested_buf, "w") as nested:
            for i in range(2):
                info = zipfile.ZipInfo(f"inner-activity-{i}.fit")
                info.compress_type = zipfile.ZIP_STORED
                nested.writestr(info, b"\x00" * 800)
        zip_path = self.root / "nested-total.zip"
        outer_buf = BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as outer:
            info = zipfile.ZipInfo("outer-activity.fit")
            info.compress_type = zipfile.ZIP_STORED
            outer.writestr(info, b"\x00" * 800)
            outer.writestr("nested.zip", nested_buf.getvalue())
        zip_path.write_bytes(outer_buf.getvalue())
        with (
            patch("trainingos.ingestion.fit.MAX_ZIP_MEMBER_BYTES", 4096),
            patch("trainingos.ingestion.fit.MAX_ZIP_TOTAL_BYTES", 2000),
        ):
            with self.assertRaises(SyncError) as cm:
                ManualFitAdapter((zip_path,))
        self.assertEqual("fit_zip_total_size_exceeded", cm.exception.code)

    # --- corrupt archives ---

    def test_corrupt_outer_zip_raises_fit_zip_unreadable(self) -> None:
        zip_path = self.root / "corrupt.zip"
        zip_path.write_bytes(b"\x00garbage\xff\xfe")
        with self.assertRaises(SyncError) as cm:
            ManualFitAdapter((zip_path,))
        self.assertEqual("fit_zip_unreadable", cm.exception.code)
        self.assertNotIn(str(zip_path), str(cm.exception))
        self.assertNotIn("garbage", str(cm.exception))

    def test_corrupt_member_in_outer_zip_raises_fit_zip_member_unreadable(self) -> None:
        zip_path = self.root / "outer.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("activity.fit", b"fit bytes")
        with patch.object(zipfile.ZipFile, "read", side_effect=OSError("simulated read error")):
            with self.assertRaises(SyncError) as cm:
                ManualFitAdapter((zip_path,))
        self.assertEqual("fit_zip_member_unreadable", cm.exception.code)

    def test_corrupt_nested_zip_raises_fit_nested_zip_unreadable(self) -> None:
        zip_path = self.root / "outer.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("nested.zip", b"\x00not a zip\xff")
        with self.assertRaises(SyncError) as cm:
            ManualFitAdapter((zip_path,))
        self.assertEqual("fit_nested_zip_unreadable", cm.exception.code)

    # --- discovery summary ---

    def test_discovery_summary_flat_zip(self) -> None:
        zip_path = self.root / "flat.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.fit", b"fit1")
            zf.writestr("b.fit", b"fit2")
            zf.writestr("notes.json", b"{}")
            zf.writestr("data.csv", b"a,b")
            zf.writestr("readme.txt", b"text")
        adapter = ManualFitAdapter((zip_path,))
        self.assertEqual(2, adapter.discovery_summary.fit_count)
        self.assertEqual(3, adapter.discovery_summary.skipped_count)
        self.assertEqual(0, adapter.discovery_summary.nested_zip_count)

    def test_discovery_summary_nested_zip(self) -> None:
        zip_path = self.root / "export.zip"
        nested_bytes = BytesIO()
        with zipfile.ZipFile(nested_bytes, "w") as nested:
            nested.writestr("run1.fit", b"fit1")
            nested.writestr("run2.fit", b"fit2")
            nested.writestr("run3.fit", b"fit3")
            nested.writestr("metadata.json", b"{}")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("outer.fit", b"fit4")
            zf.writestr("activities.zip", nested_bytes.getvalue())
        adapter = ManualFitAdapter((zip_path,))
        self.assertEqual(4, adapter.discovery_summary.fit_count)
        self.assertEqual(1, adapter.discovery_summary.skipped_count)
        self.assertEqual(1, adapter.discovery_summary.nested_zip_count)


if __name__ == "__main__":
    unittest.main()
