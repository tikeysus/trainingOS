"""Tests for GarminConnectClient and the daily_sync pipeline (issue #32)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from trainingos.config import AppConfig
from trainingos.ingestion import (
    GarminActivityAdapter,
    GarminActivityPage,
    GarminActivitySummary,
    SyncDisposition,
    SyncError,
    SyncOptions,
    SyncRecord,
    SyncReport,
    SyncRunner,
    SyncStatus,
)
from trainingos.ingestion.garmin import (
    GARMIN_EMAIL_ENV,
    GARMIN_PASSWORD_ENV,
    GarminConnectClient,
)
from trainingos.ingestion.daily_sync import GRAFANA_RUNTIME_PATH, main as daily_sync_main
from trainingos.storage import apply_migrations, connect_database

# ---------------------------------------------------------------------------
# Shared fixtures — realistic Garmin activity dicts, no real credentials
# ---------------------------------------------------------------------------

_ACTIVITY_1 = {
    "activityId": 12345678901,
    "activityName": "Morning Run",
    "startTimeGMT": "2026-06-16 10:30:00",
    "lastModified": "2026-06-16 12:00:00",
    "distance": 10000.0,
    "duration": 3600.0,
    "averageHR": 150,
}
_ACTIVITY_2 = {
    "activityId": 12345678902,
    "activityName": "Evening Jog",
    "startTimeGMT": "2026-06-17 18:00:00",
    "lastModified": "2026-06-17 20:00:00",
    "distance": 6000.0,
    "duration": 2000.0,
    "averageHR": 145,
}

_FAKE_EMAIL = "runner@example.com"
_FAKE_PASSWORD = "hunter2"
_FAKE_CREDS = {GARMIN_EMAIL_ENV: _FAKE_EMAIL, GARMIN_PASSWORD_ENV: _FAKE_PASSWORD}


def _stub_report(
    *,
    status: SyncStatus = SyncStatus.COMPLETED,
    imported_count: int = 0,
    cursor_end: str | None = None,
) -> SyncReport:
    return SyncReport(
        sync_run_id="stub-run-id",
        source="garmin",
        status=status,
        cursor_start=None,
        cursor_end=cursor_end,
        imported_count=imported_count,
        skipped_count=0,
        failed_count=0,
        dry_run=False,
    )


# ===========================================================================
# A–E: GarminConnectClient — adapter boundary (garminconnect mocked)
# ===========================================================================


class GarminConnectClientTests(unittest.TestCase):
    """GarminConnectClient maps garminconnect dicts to GarminActivitySummary objects."""

    def setUp(self) -> None:
        self._creds = patch.dict("os.environ", _FAKE_CREDS, clear=False)
        self._creds.start()

    def tearDown(self) -> None:
        self._creds.stop()

    def _client(self, activities: list[dict]) -> tuple[GarminConnectClient, MagicMock]:
        instance = MagicMock()
        instance.get_activities.return_value = activities
        garmin_cls = MagicMock(return_value=instance)
        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
        return client, instance

    # A. Happy Path

    def test_maps_activity_to_summary_with_correct_fields(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertEqual(1, len(page.activities))
        summary = page.activities[0]
        self.assertIsInstance(summary, GarminActivitySummary)
        self.assertEqual("12345678901", summary.external_id)
        self.assertIsNotNone(summary.updated_at.tzinfo)
        self.assertEqual(_ACTIVITY_1, summary.payload)

    def test_page_done_when_fewer_results_than_limit(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertTrue(page.done)

    def test_page_not_done_when_results_equal_limit(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1, _ACTIVITY_2]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=2)

        self.assertFalse(page.done)

    def test_updated_at_is_utc_aware(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertEqual(UTC, page.activities[0].updated_at.tzinfo)

    def test_all_common_fields_pass_through_payload_unchanged(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertIs(_ACTIVITY_1, page.activities[0].payload)

    # B. Boundary Values

    def test_empty_activity_list_returns_done_page(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = []
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertEqual((), page.activities)
        self.assertTrue(page.done)

    def test_large_batch_size_forwarded_to_garmin_api(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = []
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            client.fetch_activity_summaries(cursor=None, limit=200)

        args = instance.get_activities.call_args
        all_values = list(args.args) + list(args.kwargs.values())
        self.assertIn(200, all_values)

    # C. Equivalence Partitioning

    def test_activity_with_only_required_fields_maps_correctly(self) -> None:
        minimal = {"activityId": 99999, "lastModified": "2026-06-20 08:00:00"}
        instance = MagicMock()
        instance.get_activities.return_value = [minimal]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertEqual("99999", page.activities[0].external_id)

    def test_cursor_none_calls_garmin_api(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            client.fetch_activity_summaries(cursor=None, limit=50)

        instance.get_activities.assert_called_once()

    def test_multiple_activities_preserve_order(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1, _ACTIVITY_2]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertEqual("12345678901", page.activities[0].external_id)
        self.assertEqual("12345678902", page.activities[1].external_id)

    # D. Edge Cases

    def test_single_activity_maps_to_single_summary(self) -> None:
        instance = MagicMock()
        instance.get_activities.return_value = [_ACTIVITY_1]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            page = client.fetch_activity_summaries(cursor=None, limit=50)

        self.assertEqual(1, len(page.activities))

    # E. Error / Negative Tests

    def test_missing_email_env_var_raises_value_error(self) -> None:
        with patch.dict("os.environ", {GARMIN_EMAIL_ENV: ""}, clear=False):
            with patch("trainingos.ingestion.garmin.Garmin"):
                with self.assertRaises(ValueError):
                    GarminConnectClient()

    def test_missing_password_env_var_raises_value_error(self) -> None:
        with patch.dict("os.environ", {GARMIN_PASSWORD_ENV: ""}, clear=False):
            with patch("trainingos.ingestion.garmin.Garmin"):
                with self.assertRaises(ValueError):
                    GarminConnectClient()

    def test_missing_email_raises_value_error_not_key_error(self) -> None:
        import os
        env = {k: v for k, v in os.environ.items() if k != GARMIN_EMAIL_ENV}
        with patch.dict("os.environ", env, clear=True):
            with patch("trainingos.ingestion.garmin.Garmin"):
                with self.assertRaises(ValueError):
                    GarminConnectClient()

    def test_activity_missing_activity_id_raises_sync_error(self) -> None:
        bad = {"lastModified": "2026-06-16 12:00:00", "activityName": "No ID"}
        instance = MagicMock()
        instance.get_activities.return_value = [bad]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            with self.assertRaises(SyncError):
                client.fetch_activity_summaries(cursor=None, limit=50)

    def test_activity_missing_timestamp_raises_sync_error(self) -> None:
        bad = {"activityId": 99999, "activityName": "No Timestamp"}
        instance = MagicMock()
        instance.get_activities.return_value = [bad]
        garmin_cls = MagicMock(return_value=instance)

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            client = GarminConnectClient()
            with self.assertRaises(SyncError):
                client.fetch_activity_summaries(cursor=None, limit=50)

    def test_credentials_absent_from_error_when_garmin_constructor_raises(self) -> None:
        garmin_cls = MagicMock(
            side_effect=Exception(
                f"login failed for {_FAKE_EMAIL} with password {_FAKE_PASSWORD}"
            )
        )

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            try:
                client = GarminConnectClient()
                client.fetch_activity_summaries(cursor=None, limit=50)
                self.fail("expected an error when garminconnect raises")
            except (SyncError, ValueError) as error:
                error_text = str(error)
                self.assertNotIn(_FAKE_EMAIL, error_text)
                self.assertNotIn(_FAKE_PASSWORD, error_text)

    def test_auth_failure_raises_retryable_sync_error(self) -> None:
        garmin_cls = MagicMock(
            side_effect=Exception("session expired")
        )

        with patch("trainingos.ingestion.garmin.Garmin", garmin_cls):
            try:
                client = GarminConnectClient()
                client.fetch_activity_summaries(cursor=None, limit=50)
                self.fail("expected SyncError on auth failure")
            except SyncError as error:
                self.assertTrue(error.retryable)


# ===========================================================================
# F (state) + pipeline: GarminActivityAdapter + SyncRunner with stub handler
# These test cursor logic, idempotency, and run transitions without main().
# ===========================================================================


class _StubGarminClient:
    """Controllable GarminClient for adapter + runner integration tests."""

    def __init__(self, pages: list[GarminActivityPage]) -> None:
        self._pages = list(pages)
        self.cursors_received: list[str | None] = []

    def fetch_activity_summaries(
        self, cursor: str | None, limit: int
    ) -> GarminActivityPage:
        self.cursors_received.append(cursor)
        if self._pages:
            return self._pages.pop(0)
        return GarminActivityPage(activities=(), done=True)


def _activity_summary(
    activity_id: str,
    updated_at: datetime,
    payload: dict,
) -> GarminActivitySummary:
    return GarminActivitySummary(
        external_id=activity_id,
        updated_at=updated_at,
        payload=payload,
    )


class GarminAdapterSyncTests(unittest.TestCase):
    """GarminActivityAdapter + SyncRunner with a stub handler."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "training.sqlite3"
        self._connection = connect_database(db_path)
        apply_migrations(self._connection)
        self._connection.execute(
            """
            CREATE TABLE garmin_stub_imports (
                external_id TEXT PRIMARY KEY
            )
            """
        )
        self._connection.commit()

        self._runner = SyncRunner(
            self._connection,
            clock=lambda: datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
            sleeper=lambda _: None,
        )

    def tearDown(self) -> None:
        self._connection.close()
        self._tmpdir.cleanup()

    def _idempotent_handler(
        self,
        connection: sqlite3.Connection,
        record: SyncRecord[GarminActivitySummary],
    ) -> SyncDisposition:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO garmin_stub_imports (external_id) VALUES (?)",
            (record.external_id,),
        )
        return SyncDisposition.IMPORTED if cursor.rowcount else SyncDisposition.SKIPPED

    def _summary_page(
        self, activities: list[tuple[str, datetime]], done: bool = True
    ) -> GarminActivityPage:
        return GarminActivityPage(
            activities=tuple(
                _activity_summary(aid, ts, {"activityId": int(aid)})
                for aid, ts in activities
            ),
            done=done,
        )

    # F. State-Based

    def test_sync_run_transitions_to_completed_on_clean_sync(self) -> None:
        page = self._summary_page([("12345678901", datetime(2026, 6, 16, 12, 0, tzinfo=UTC))])
        client = _StubGarminClient([page])
        adapter = GarminActivityAdapter(client)

        report = self._runner.run(adapter, self._idempotent_handler)

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.imported_count)

    def test_sync_run_transitions_to_failed_on_nonretryable_error(self) -> None:
        class FailingClient:
            def fetch_activity_summaries(self, cursor, limit):
                raise SyncError("auth_failed", "invalid session", retryable=False)

        adapter = GarminActivityAdapter(FailingClient())
        report = self._runner.run(adapter, self._idempotent_handler)

        self.assertEqual(SyncStatus.FAILED, report.status)

    def test_cursor_advances_to_last_imported_activity(self) -> None:
        page = self._summary_page(
            [
                ("12345678901", datetime(2026, 6, 16, 12, 0, tzinfo=UTC)),
                ("12345678902", datetime(2026, 6, 17, 20, 0, tzinfo=UTC)),
            ]
        )
        client = _StubGarminClient([page])
        adapter = GarminActivityAdapter(client)

        self._runner.run(adapter, self._idempotent_handler)

        row = self._connection.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = 'garmin'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("12345678902", row["cursor"])

    def test_idempotent_double_run_skips_already_imported_activities(self) -> None:
        page = self._summary_page([("12345678901", datetime(2026, 6, 16, 12, 0, tzinfo=UTC))])
        client = _StubGarminClient([page])
        adapter = GarminActivityAdapter(client)
        first = self._runner.run(adapter, self._idempotent_handler)

        overlap_page = GarminActivityPage(
            activities=(
                _activity_summary(
                    "12345678901",
                    datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
                    {"activityId": 12345678901},
                ),
            ),
            done=True,
        )
        client2 = _StubGarminClient([overlap_page])
        adapter2 = GarminActivityAdapter(client2)
        second = self._runner.run(adapter2, self._idempotent_handler)

        self.assertEqual(SyncStatus.COMPLETED, first.status)
        self.assertEqual(SyncStatus.COMPLETED, second.status)
        self.assertEqual(1, first.imported_count)
        # Re-processed from overlapping window — skipped because already present.
        self.assertEqual(0, second.imported_count)
        self.assertEqual(
            1,
            self._connection.execute(
                "SELECT COUNT(*) FROM garmin_stub_imports"
            ).fetchone()[0],
        )

    def test_second_run_receives_cursor_from_first_run(self) -> None:
        page1 = self._summary_page([("12345678901", datetime(2026, 6, 16, 12, 0, tzinfo=UTC))])
        client = _StubGarminClient([page1])
        adapter = GarminActivityAdapter(client)
        self._runner.run(adapter, self._idempotent_handler)

        client2 = _StubGarminClient([GarminActivityPage(activities=(), done=True)])
        adapter2 = GarminActivityAdapter(client2)
        self._runner.run(adapter2, self._idempotent_handler)

        self.assertIsNone(client.cursors_received[0])
        self.assertIsNotNone(client2.cursors_received[0])
        self.assertIn("12345678901", client2.cursors_received[0])


# ===========================================================================
# daily_sync.main() — pipeline orchestration (SyncRunner and refresh mocked)
# ===========================================================================


class DailySyncOrchestrationTests(unittest.TestCase):
    """daily_sync.main() wires migrations → sync → refresh → Grafana copy."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._root = Path(self._tmpdir.name)
        self._database_path = self._root / "training.sqlite3"
        self._grafana_dir = self._root / "grafana-runtime"
        self._grafana_target = self._grafana_dir / "trainingos.sqlite3"

        conn = connect_database(self._database_path)
        apply_migrations(conn)
        conn.close()

        self._env = {
            **_FAKE_CREDS,
            "TRAININGOS_DB_PATH": str(self._database_path),
            "TRAININGOS_LOCAL_TIMEZONE": "UTC",
        }

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _fake_client_cls(self) -> MagicMock:
        """Mock GarminConnectClient class; instance returns empty done page."""
        instance = MagicMock()
        instance.fetch_activity_summaries.return_value = GarminActivityPage(
            activities=(), done=True
        )
        return MagicMock(return_value=instance)

    def _run(
        self,
        *,
        runner_return: SyncReport | None = None,
        grafana_dir_exists: bool = False,
    ) -> tuple[int, MagicMock, MagicMock]:
        if grafana_dir_exists:
            self._grafana_dir.mkdir(exist_ok=True)

        report = runner_return or _stub_report()
        mock_runner_cls = MagicMock()
        mock_runner_cls.return_value.run.return_value = report
        mock_refresh = MagicMock()

        with (
            patch.dict("os.environ", self._env, clear=False),
            patch("trainingos.ingestion.daily_sync.GarminConnectClient", self._fake_client_cls()),
            patch("trainingos.ingestion.daily_sync.SyncRunner", mock_runner_cls),
            patch("trainingos.ingestion.daily_sync.refresh_training_data", mock_refresh),
            patch("trainingos.ingestion.daily_sync.GRAFANA_RUNTIME_PATH", self._grafana_target),
        ):
            exit_code = daily_sync_main()

        return exit_code, mock_runner_cls, mock_refresh

    # A. Happy Path

    def test_full_pipeline_exits_zero_on_success(self) -> None:
        exit_code, _, _ = self._run(grafana_dir_exists=True)
        self.assertEqual(0, exit_code)

    def test_refresh_called_after_sync(self) -> None:
        call_order: list[str] = []

        mock_runner_cls = MagicMock()
        mock_runner_cls.return_value.run.side_effect = lambda *a, **kw: (
            call_order.append("sync") or _stub_report()
        )
        mock_refresh = MagicMock(side_effect=lambda *a, **kw: call_order.append("refresh"))

        with (
            patch.dict("os.environ", self._env, clear=False),
            patch("trainingos.ingestion.daily_sync.GarminConnectClient", self._fake_client_cls()),
            patch("trainingos.ingestion.daily_sync.SyncRunner", mock_runner_cls),
            patch("trainingos.ingestion.daily_sync.refresh_training_data", mock_refresh),
            patch("trainingos.ingestion.daily_sync.GRAFANA_RUNTIME_PATH", self._grafana_target),
        ):
            daily_sync_main()

        self.assertEqual(["sync", "refresh"], call_order)

    def test_grafana_copy_written_to_runtime_path_when_dir_exists(self) -> None:
        self._run(grafana_dir_exists=True)
        self.assertTrue(self._grafana_target.exists())

    def test_grafana_copy_is_valid_sqlite_file(self) -> None:
        self._run(grafana_dir_exists=True)
        conn = connect_database(self._grafana_target)
        conn.close()

    def test_sync_run_row_recorded_as_completed(self) -> None:
        self._run()

        conn = connect_database(self._database_path)
        row = conn.execute(
            "SELECT status FROM sync_runs WHERE source = 'garmin'"
        ).fetchone()
        conn.close()
        self.assertEqual("completed", row["status"])

    # B. Boundary Values

    def test_zero_new_activities_exits_zero(self) -> None:
        exit_code, _, refresh = self._run(runner_return=_stub_report(imported_count=0))
        self.assertEqual(0, exit_code)
        refresh.assert_called_once()

    # C. Equivalence Partitioning — Grafana dir absent vs present

    def test_grafana_dir_absent_skips_copy_silently(self) -> None:
        exit_code, _, _ = self._run(grafana_dir_exists=False)
        self.assertEqual(0, exit_code)
        self.assertFalse(self._grafana_target.exists())

    def test_sync_failure_exits_one(self) -> None:
        failed = _stub_report(status=SyncStatus.FAILED)
        exit_code, _, _ = self._run(runner_return=failed)
        self.assertEqual(1, exit_code)

    def test_refresh_failure_exits_one(self) -> None:
        mock_runner_cls = MagicMock()
        mock_runner_cls.return_value.run.return_value = _stub_report()

        with (
            patch.dict("os.environ", self._env, clear=False),
            patch("trainingos.ingestion.daily_sync.GarminConnectClient", self._fake_client_cls()),
            patch("trainingos.ingestion.daily_sync.SyncRunner", mock_runner_cls),
            patch(
                "trainingos.ingestion.daily_sync.refresh_training_data",
                side_effect=RuntimeError("refresh exploded"),
            ),
            patch("trainingos.ingestion.daily_sync.GRAFANA_RUNTIME_PATH", self._grafana_target),
        ):
            exit_code = daily_sync_main()

        self.assertEqual(1, exit_code)

    # D. Edge Cases — best-effort Grafana copy

    def test_grafana_copy_failure_does_not_fail_sync(self) -> None:
        mock_runner_cls = MagicMock()
        mock_runner_cls.return_value.run.return_value = _stub_report()
        self._grafana_dir.mkdir()

        with (
            patch.dict("os.environ", self._env, clear=False),
            patch("trainingos.ingestion.daily_sync.GarminConnectClient", self._fake_client_cls()),
            patch("trainingos.ingestion.daily_sync.SyncRunner", mock_runner_cls),
            patch("trainingos.ingestion.daily_sync.refresh_training_data"),
            patch("trainingos.ingestion.daily_sync.GRAFANA_RUNTIME_PATH", self._grafana_target),
            patch(
                "trainingos.ingestion.daily_sync.shutil.copy2",
                side_effect=OSError("permission denied"),
            ),
        ):
            exit_code = daily_sync_main()

        self.assertEqual(0, exit_code)

    # E. Credential safety — no email/password in sync_errors table

    def test_credentials_absent_from_sync_errors_table(self) -> None:
        garmin_instance = MagicMock()
        garmin_instance.fetch_activity_summaries.side_effect = Exception(
            f"session expired for {_FAKE_EMAIL}"
        )
        fake_client_cls = MagicMock(return_value=garmin_instance)

        with (
            patch.dict("os.environ", self._env, clear=False),
            patch("trainingos.ingestion.daily_sync.GarminConnectClient", fake_client_cls),
            patch("trainingos.ingestion.daily_sync.GRAFANA_RUNTIME_PATH", self._grafana_target),
            patch("trainingos.ingestion.daily_sync.refresh_training_data"),
        ):
            daily_sync_main()

        conn = connect_database(self._database_path)
        rows = conn.execute("SELECT message FROM sync_errors").fetchall()
        conn.close()
        all_messages = " ".join(row["message"] for row in rows)
        self.assertNotIn(_FAKE_EMAIL, all_messages)
        self.assertNotIn(_FAKE_PASSWORD, all_messages)

    # F. State — migrations applied before sync

    def test_migrations_applied_before_sync_runner_is_created(self) -> None:
        """SyncRunner construction implies the schema is already present."""
        construction_order: list[str] = []

        original_apply = __import__("trainingos.storage", fromlist=["apply_migrations"]).apply_migrations

        def tracking_migrations(conn):
            construction_order.append("migrations")
            return original_apply(conn)

        mock_runner_cls = MagicMock()

        def tracking_runner(*args, **kwargs):
            construction_order.append("runner")
            return mock_runner_cls(*args, **kwargs)

        mock_runner_cls.return_value.run.return_value = _stub_report()

        with (
            patch.dict("os.environ", self._env, clear=False),
            patch("trainingos.ingestion.daily_sync.GarminConnectClient", self._fake_client_cls()),
            patch("trainingos.ingestion.daily_sync.apply_migrations", tracking_migrations),
            patch("trainingos.ingestion.daily_sync.SyncRunner", tracking_runner),
            patch("trainingos.ingestion.daily_sync.refresh_training_data"),
            patch("trainingos.ingestion.daily_sync.GRAFANA_RUNTIME_PATH", self._grafana_target),
        ):
            daily_sync_main()

        self.assertEqual("migrations", construction_order[0])
        self.assertEqual("runner", construction_order[1])


if __name__ == "__main__":
    unittest.main()
