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
    SyncRunner,
    SyncStatus,
    parse_fit_messages,
)
from trainingos.ingestion import fit_import
from trainingos.ingestion.fit import classify_fit_file, FitFileClass
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

    def test_classify_activity_type_string_returns_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"type": "activity"}),
        )
        result = classify_fit_file(messages)
        self.assertTrue(result.is_activity)

    def test_classify_device_type_returns_non_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"type": "device"}),
        )
        result = classify_fit_file(messages)
        self.assertFalse(result.is_activity)
        self.assertIn("device", result.reason)

    def test_classify_settings_type_returns_non_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"type": "settings"}),
        )
        result = classify_fit_file(messages)
        self.assertFalse(result.is_activity)

    def test_classify_course_type_returns_non_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"type": "course"}),
        )
        result = classify_fit_file(messages)
        self.assertFalse(result.is_activity)

    def test_classify_workout_type_returns_non_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"type": "workout"}),
        )
        result = classify_fit_file(messages)
        self.assertFalse(result.is_activity)

    def test_classify_without_file_id_message_falls_through_to_activity(self) -> None:
        messages = (
            FitMessage("session", {"sport": "running"}),
        )
        result = classify_fit_file(messages)
        self.assertTrue(result.is_activity)

    def test_classify_file_id_without_type_field_falls_through_to_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"serial_number": 99999}),
        )
        result = classify_fit_file(messages)
        self.assertTrue(result.is_activity)

    def test_classify_monitoring_type_returns_non_activity(self) -> None:
        messages = (
            FitMessage("file_id", {"type": "monitoring_a"}),
        )
        result = classify_fit_file(messages)
        self.assertFalse(result.is_activity)

    def test_handler_skips_device_fit_without_retaining_raw_artifact(self) -> None:
        fit_path = self.root / "device.fit"
        fit_path.write_bytes(b"device fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=(FitMessage("file_id", {"type": "device"}),),
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.skipped_count)
        self.assertEqual(0, self._count("raw_source_records"))
        self.assertEqual(0, self._count("activities"))

    def test_handler_skips_course_fit_without_retaining_raw_artifact(self) -> None:
        fit_path = self.root / "course.fit"
        fit_path.write_bytes(b"course fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=(FitMessage("file_id", {"type": "course"}),),
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.skipped_count)
        self.assertEqual(0, self._count("raw_source_records"))
        self.assertEqual(0, self._count("activities"))

    def test_handler_skips_workout_fit_without_retaining_raw_artifact(self) -> None:
        fit_path = self.root / "workout.fit"
        fit_path.write_bytes(b"workout fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=(FitMessage("file_id", {"type": "workout"}),),
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.skipped_count)
        self.assertEqual(0, self._count("raw_source_records"))
        self.assertEqual(0, self._count("activities"))

    def test_handler_skips_settings_fit_without_retaining_raw_artifact(self) -> None:
        fit_path = self.root / "settings.fit"
        fit_path.write_bytes(b"settings fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=(FitMessage("file_id", {"type": "settings"}),),
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.skipped_count)
        self.assertEqual(0, self._count("raw_source_records"))
        self.assertEqual(0, self._count("activities"))

    def test_mixed_export_zip_counts_activity_and_non_activity_correctly(self) -> None:
        export_zip = self.root / "garmin-export.zip"
        with zipfile.ZipFile(export_zip, "w") as archive:
            archive.writestr("a-device.fit", b"device fit")
            archive.writestr("b-course.fit", b"course fit")
            archive.writestr("c-activity.fit", b"activity fit")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )
        device_messages = (FitMessage("file_id", {"type": "device"}),)
        course_messages = (FitMessage("file_id", {"type": "course"}),)

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            side_effect=(device_messages, course_messages, self._messages()),
        ):
            report = runner.run(ManualFitAdapter((export_zip,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.imported_count)
        self.assertEqual(2, report.skipped_count)
        self.assertEqual(1, self._count("activities"))
        self.assertEqual(1, self._count("raw_source_records"))

    def test_skip_reason_recorded_for_non_activity_fit(self) -> None:
        fit_path = self.root / "device.fit"
        fit_path.write_bytes(b"device fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="America/Toronto",
            clock=self.clock,
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=(FitMessage("file_id", {"type": "device"}),),
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(1, report.skipped_count)
        # Deliberate non-activity skips must not be recorded as errors.
        error = self.connection.execute(
            "SELECT error_code FROM sync_errors WHERE sync_run_id = ?",
            (report.sync_run_id,),
        ).fetchone()
        self.assertIsNone(error)
        # TODO: tighten once the skip-reason API is finalised. If SyncReport
        # gains a skip_reasons dict (e.g. {"non_activity_file_type": 1}), assert
        # that here. For now we verify the count and the absence of an error row.
        # Example of what a future assertion might look like:
        #   self.assertIn("device", report.skip_reasons.get("non_activity_file_type", ""))

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

    def test_standalone_non_activity_fit_raises_or_skips_clearly_without_raw_artifact(
        self,
    ) -> None:
        # Design TBD: Option A raises SyncError (strict), Option B returns SKIPPED.
        # Either way the import must not silently succeed and must write nothing.
        fit_path = self.root / "device.fit"
        fit_path.write_bytes(b"device fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="UTC",
            clock=self.clock,
        )
        device_messages = (
            FitMessage("file_id", {"type": "device", "serial_number": 11111}),
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=device_messages,
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        is_strict_failure = (
            report.status == SyncStatus.FAILED
            and report.failed_count == 1
        )
        is_deliberate_skip = report.skipped_count == 1

        self.assertTrue(
            is_strict_failure or is_deliberate_skip,
            f"expected explicit failure or deliberate skip, got status={report.status!r} "
            f"failed={report.failed_count} skipped={report.skipped_count}",
        )
        self.assertEqual(0, self._count("raw_source_records"))
        self.assertEqual(0, self._count("activities"))

        if is_strict_failure:
            error = self.connection.execute(
                "SELECT error_code FROM sync_errors WHERE sync_run_id = ?",
                (report.sync_run_id,),
            ).fetchone()
            self.assertIn(
                error["error_code"],
                {"fit_non_activity", "fit_session_missing"},
            )

    def test_standalone_truly_malformed_fit_still_fails(self) -> None:
        fit_path = self.root / "malformed2.fit"
        fit_path.write_bytes(b"corrupted fit bytes")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="UTC",
            clock=self.clock,
        )

        # Empty message set → no session → fit_session_missing; stands in for
        # the parse-failed path because standalone never has skip_missing_session.
        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=(),
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual(1, report.failed_count)
        self.assertEqual(0, self._count("raw_source_records"))

        error = self.connection.execute(
            "SELECT error_code, message FROM sync_errors WHERE sync_run_id = ?",
            (report.sync_run_id,),
        ).fetchone()
        self.assertIsNotNone(error, "expected a sync_errors row for the failure")
        self.assertIn(
            error["error_code"],
            {"fit_parse_failed", "fit_session_missing"},
        )
        # Message must be a human-readable string, not a raw traceback dump.
        self.assertNotIn("Traceback", error["message"])

    def test_standalone_missing_session_activity_type_fails_strictly(
        self,
    ) -> None:
        fit_path = self.root / "activity-nosession.fit"
        fit_path.write_bytes(b"activity fit no session")
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="UTC",
            clock=self.clock,
        )
        # file_id says "activity" but there is no session message.
        activity_no_session = (
            FitMessage(
                "file_id",
                {
                    "type": "activity",
                    "serial_number": 22222,
                },
            ),
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=activity_no_session,
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        # standalone → skip_missing_session=False → must fail, never silently skip
        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual(1, report.failed_count)
        self.assertEqual(0, self._count("raw_source_records"))

        error = self.connection.execute(
            "SELECT error_code FROM sync_errors WHERE sync_run_id = ?",
            (report.sync_run_id,),
        ).fetchone()
        self.assertEqual("fit_session_missing", error["error_code"])

    def test_zip_non_activity_fit_is_skipped_not_failed(self) -> None:
        # Contrasts with standalone tests: zip sets skip_missing_session=True,
        # so a device file with no session is a skip, not a failure.
        export_zip = self.root / "garmin-mixed.zip"
        with zipfile.ZipFile(export_zip, "w") as archive:
            archive.writestr("a-device-settings.fit", b"device fit bytes")
            archive.writestr("b-run-activity.fit", b"activity fit bytes")

        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="UTC",
            clock=self.clock,
        )
        device_messages = (
            FitMessage("file_id", {"type": "device", "serial_number": 33333}),
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            side_effect=(device_messages, self._messages()),
        ):
            report = runner.run(ManualFitAdapter((export_zip,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(0, report.failed_count)
        self.assertGreaterEqual(report.skipped_count, 1)
        self.assertEqual(1, self._count("activities"))

    def test_classify_fit_file_error_message_does_not_leak_file_content(
        self,
    ) -> None:
        fit_path = self.root / "course.fit"
        fit_bytes = b"course fit bytes that must not appear in error logs"
        fit_path.write_bytes(fit_bytes)
        runner = SyncRunner(self.connection, clock=self.clock)
        handler = ManualFitHandler(
            RawArtifactStore(self.raw_dir),
            timezone="UTC",
            clock=self.clock,
        )
        course_messages = (
            FitMessage("file_id", {"type": "course", "serial_number": 44444}),
        )

        with patch(
            "trainingos.ingestion.fit.read_fit_messages",
            return_value=course_messages,
        ):
            report = runner.run(ManualFitAdapter((fit_path,)), handler)

        error = self.connection.execute(
            "SELECT message FROM sync_errors WHERE sync_run_id = ?",
            (report.sync_run_id,),
        ).fetchone()
        if error is not None:
            self.assertNotIn(
                fit_bytes.decode(errors="replace"),
                error["message"],
                "raw file bytes must not appear in the error message",
            )
            self.assertNotIn(
                str(fit_path),
                error["message"],
                "raw filesystem path must not appear in the error message",
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


if __name__ == "__main__":
    unittest.main()
