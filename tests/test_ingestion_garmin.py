from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from trainingos.ingestion import (
    GarminActivityAdapter,
    GarminActivityPage,
    GarminActivitySummary,
    RetryPolicy,
    SyncDisposition,
    SyncOptions,
    SyncRunner,
    SyncStatus,
)
from trainingos.storage import apply_migrations, connect_database


@dataclass(slots=True)
class FakeGarminClient:
    page: GarminActivityPage
    calls: list[tuple[str | None, int]]

    def fetch_activity_summaries(
        self,
        cursor: str | None,
        limit: int,
    ) -> GarminActivityPage:
        self.calls.append((cursor, limit))
        return self.page


class GarminIngestionTests(unittest.TestCase):
    def test_adapter_maps_client_page_to_sync_records(self) -> None:
        activity = GarminActivitySummary(
            external_id="garmin-activity-1",
            updated_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
            payload={"activityName": "Sanitized Run"},
        )
        client = FakeGarminClient(
            GarminActivityPage((activity,), done=True),
            calls=[],
        )

        page = GarminActivityAdapter(client).fetch("checkpoint", 50)

        self.assertTrue(page.done)
        self.assertEqual([("checkpoint", 50)], client.calls)
        self.assertEqual("garmin-activity-1", page.records[0].external_id)
        self.assertEqual(activity, page.records[0].payload)
        self.assertEqual(
            "2026-06-16T12:00:00+00:00|garmin-activity-1",
            page.records[0].cursor_after,
        )

    def test_adapter_compares_cursors_without_credentials_or_payloads(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        self.assertTrue(
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-1",
                "2026-06-16T12:00:00+00:00|activity-2",
            )
        )
        self.assertFalse(
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-2",
                "2026-06-16T12:00:00+00:00|activity-1",
            )
        )

    def test_cursor_normalizes_non_utc_timezone_to_utc(self) -> None:
        utc_plus_5 = timezone(timedelta(hours=5))
        activity = GarminActivitySummary(
            external_id="garmin-activity-tz",
            updated_at=datetime(2026, 6, 16, 17, 0, tzinfo=utc_plus_5),
            payload={},
        )
        client = FakeGarminClient(
            GarminActivityPage((activity,), done=True),
            calls=[],
        )

        page = GarminActivityAdapter(client).fetch(None, 10)

        self.assertEqual(
            "2026-06-16T12:00:00+00:00|garmin-activity-tz",
            page.records[0].cursor_after,
        )

    def test_cursor_comparison_same_timestamp_later_id_is_at_or_after(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        self.assertTrue(
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-abc",
                "2026-06-16T12:00:00+00:00|activity-abd",
            )
        )
        self.assertFalse(
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-abd",
                "2026-06-16T12:00:00+00:00|activity-abc",
            )
        )

    def test_cursor_comparison_later_timestamp_wins_regardless_of_id(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        # candidate has a later timestamp but a lexicographically earlier ID
        self.assertTrue(
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-zzz",
                "2026-06-16T13:00:00+00:00|activity-aaa",
            )
        )

    def test_cursor_comparison_earlier_timestamp_is_not_at_or_after(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        self.assertFalse(
            adapter.cursor_is_at_or_after(
                "2026-06-16T13:00:00+00:00|activity-1",
                "2026-06-16T12:00:00+00:00|activity-1",
            )
        )

    def test_cursor_comparison_identical_cursor_is_at_or_after(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        self.assertTrue(
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-1",
                "2026-06-16T12:00:00+00:00|activity-1",
            )
        )

    def test_malformed_cursor_missing_pipe_raises_value_error(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        with self.assertRaises(ValueError):
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-1",
                "2026-06-16T12:00:00+00:00-no-pipe",
            )

        with self.assertRaises(ValueError):
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00-no-pipe",
                "2026-06-16T12:00:00+00:00|activity-1",
            )

    def test_cursor_with_naive_timestamp_raises_value_error(self) -> None:
        adapter = GarminActivityAdapter(
            FakeGarminClient(GarminActivityPage((), done=True), calls=[])
        )

        with self.assertRaises(ValueError):
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00|activity-1",
                "2026-06-16T12:00:00+00:00|activity-1",
            )

        with self.assertRaises(ValueError):
            adapter.cursor_is_at_or_after(
                "2026-06-16T12:00:00+00:00|activity-1",
                "2026-06-16T12:00:00|activity-1",
            )

    def test_fetch_empty_page_returns_zero_records_and_done(self) -> None:
        client = FakeGarminClient(
            GarminActivityPage((), done=True),
            calls=[],
        )

        page = GarminActivityAdapter(client).fetch(None, 50)

        self.assertTrue(page.done)
        self.assertEqual((), page.records)

    def test_activity_summary_rejects_blank_external_id(self) -> None:
        with self.assertRaises(ValueError):
            GarminActivitySummary(
                external_id="",
                updated_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
                payload={},
            )

        with self.assertRaises(ValueError):
            GarminActivitySummary(
                external_id="   ",
                updated_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
                payload={},
            )

    def test_activity_summary_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            GarminActivitySummary(
                external_id="garmin-activity-naive",
                updated_at=datetime(2026, 6, 16, 12, 0),
                payload={},
            )


class GarminCredentialSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "training.sqlite3"
        self._conn = connect_database(db_path)
        apply_migrations(self._conn)
        self._runner = SyncRunner(
            self._conn,
            clock=lambda: datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
            sleeper=lambda _: None,
        )

    def tearDown(self) -> None:
        self._conn.close()
        self._tmp.cleanup()

    def _error_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM sync_errors").fetchall()

    def test_client_exception_with_credential_not_persisted(self) -> None:
        class LeakingClient:
            def fetch_activity_summaries(self, cursor: str | None, limit: int) -> GarminActivityPage:
                raise RuntimeError("password=hunter2")

        report = self._runner.run(
            GarminActivityAdapter(LeakingClient()),
            lambda conn, record: SyncDisposition.IMPORTED,
            SyncOptions(retry=RetryPolicy(max_attempts=1)),
        )

        rows = self._error_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("unexpected_error", rows[0]["error_code"])
        self.assertNotIn("hunter2", rows[0]["message"])

    def test_activity_payload_fields_not_persisted_in_error_log(self) -> None:
        calls: list[str | None] = []

        class TwoCallClient:
            def fetch_activity_summaries(self, cursor: str | None, limit: int) -> GarminActivityPage:
                calls.append(cursor)
                if len(calls) == 1:
                    activity = GarminActivitySummary(
                        external_id="garmin-activity-secret",
                        updated_at=datetime(2026, 6, 11, 10, 0, tzinfo=UTC),
                        payload={"token": "secret-token-xyz"},
                    )
                    return GarminActivityPage(activities=(activity,), done=False)
                raise RuntimeError("auth failure")

        self._runner.run(
            GarminActivityAdapter(TwoCallClient()),
            lambda conn, record: SyncDisposition.IMPORTED,
            SyncOptions(retry=RetryPolicy(max_attempts=1)),
        )

        rows = self._error_rows()
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertNotIn("secret-token-xyz", row["message"])

    def test_cursor_does_not_embed_payload_credentials(self) -> None:
        activity = GarminActivitySummary(
            external_id="garmin-activity-cursor-test",
            updated_at=datetime(2026, 6, 11, 10, 0, tzinfo=UTC),
            payload={"password": "should-not-appear"},
        )

        class StaticClient:
            def fetch_activity_summaries(self, cursor: str | None, limit: int) -> GarminActivityPage:
                return GarminActivityPage(activities=(activity,), done=True)

        page = GarminActivityAdapter(StaticClient()).fetch(None, 10)

        self.assertEqual(1, len(page.records))
        cursor = page.records[0].cursor_after
        self.assertIn("2026-06-11T10:00:00+00:00", cursor)
        self.assertIn("garmin-activity-cursor-test", cursor)
        self.assertNotIn("should-not-appear", cursor)

    def test_client_exception_wrapping_sets_unexpected_error_code(self) -> None:
        class LeakingClient:
            def fetch_activity_summaries(self, cursor: str | None, limit: int) -> GarminActivityPage:
                raise RuntimeError("password=hunter2")

        report = self._runner.run(
            GarminActivityAdapter(LeakingClient()),
            lambda conn, record: SyncDisposition.IMPORTED,
            SyncOptions(retry=RetryPolicy(max_attempts=1)),
        )

        self.assertEqual(SyncStatus.FAILED, report.status)
        rows = self._error_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("unexpected_error", rows[0]["error_code"])


if __name__ == "__main__":
    unittest.main()
