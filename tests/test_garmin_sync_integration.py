from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trainingos.ingestion import (
    GarminActivityAdapter,
    GarminActivityPage,
    GarminActivitySummary,
    SyncDisposition,
    SyncError,
    SyncOptions,
    RetryPolicy,
    SyncRunner,
    SyncStatus,
)
from trainingos.storage import apply_migrations, connect_database


@dataclass(slots=True)
class FakeGarminClient:
    pages: list[GarminActivityPage]
    calls: list[tuple[str | None, int]]

    def fetch_activity_summaries(self, cursor: str | None, limit: int) -> GarminActivityPage:
        self.calls.append((cursor, limit))
        return self.pages.pop(0)


def garmin_handler(
    connection: sqlite3.Connection,
    record,
) -> SyncDisposition:
    cursor = connection.execute(
        "INSERT OR IGNORE INTO garmin_activities (external_id, payload) VALUES (?, ?)",
        (record.external_id, json.dumps(record.payload.payload)),
    )
    return SyncDisposition.IMPORTED if cursor.rowcount else SyncDisposition.SKIPPED


def _make_activity(
    external_id: str,
    updated_at: datetime,
    payload: dict | None = None,
) -> GarminActivitySummary:
    return GarminActivitySummary(
        external_id=external_id,
        updated_at=updated_at,
        payload=payload or {"activityName": external_id},
    )


def _cursor(updated_at: datetime, external_id: str) -> str:
    return f"{updated_at.isoformat()}|{external_id}"


class GarminSyncIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "training.sqlite3"
        self.conn = connect_database(db_path)
        apply_migrations(self.conn)
        self.conn.execute(
            "CREATE TABLE garmin_activities (external_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.conn.commit()
        self.runner = SyncRunner(
            self.conn,
            clock=lambda: datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
            sleeper=lambda _: None,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _activity_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM garmin_activities").fetchone()[0]

    def _checkpoint(self) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = 'garmin'"
        ).fetchone()
        return row["cursor"] if row else None

    def test_full_sync_from_scratch_advances_checkpoint(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        t3 = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        a2 = _make_activity("garmin-2", t2)
        a3 = _make_activity("garmin-3", t3)
        client = FakeGarminClient(
            pages=[
                GarminActivityPage((a1, a2), done=False),
                GarminActivityPage((a3,), done=True),
            ],
            calls=[],
        )
        adapter = GarminActivityAdapter(client)

        report = self.runner.run(adapter, garmin_handler, SyncOptions(batch_size=2))

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(3, report.imported_count)
        self.assertEqual(_cursor(t3, "garmin-3"), self._checkpoint())
        self.assertEqual(3, self._activity_count())

    def test_second_sync_from_checkpoint_only_fetches_new_activities(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        t3 = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        a2 = _make_activity("garmin-2", t2)
        a3 = _make_activity("garmin-3", t3)
        first_client = FakeGarminClient(
            pages=[GarminActivityPage((a1, a2), done=True)],
            calls=[],
        )
        self.runner.run(GarminActivityAdapter(first_client), garmin_handler)
        checkpoint_after_first = self._checkpoint()

        second_client = FakeGarminClient(
            pages=[GarminActivityPage((a3,), done=True)],
            calls=[],
        )
        second_report = self.runner.run(GarminActivityAdapter(second_client), garmin_handler)

        self.assertEqual(checkpoint_after_first, second_report.cursor_start)
        self.assertEqual(1, second_report.imported_count)
        self.assertEqual(3, self._activity_count())

    def test_duplicate_activity_on_resync_is_skipped(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        first_client = FakeGarminClient(
            pages=[GarminActivityPage((a1,), done=True)],
            calls=[],
        )
        self.runner.run(GarminActivityAdapter(first_client), garmin_handler)

        second_client = FakeGarminClient(
            pages=[GarminActivityPage((a1,), done=True)],
            calls=[],
        )
        second_report = self.runner.run(GarminActivityAdapter(second_client), garmin_handler)

        self.assertEqual(1, second_report.skipped_count)
        self.assertEqual(0, second_report.imported_count)
        self.assertEqual(1, self._activity_count())

    def test_partial_failure_leaves_checkpoint_at_last_successful_record(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        t3 = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        a2 = _make_activity("garmin-2", t2)
        a3 = _make_activity("garmin-3", t3)

        def failing_handler(connection: sqlite3.Connection, record) -> SyncDisposition:
            if record.external_id == "garmin-3":
                raise SyncError("bad_payload", "rejected")
            return garmin_handler(connection, record)

        client = FakeGarminClient(
            pages=[GarminActivityPage((a1, a2, a3), done=True)],
            calls=[],
        )
        report = self.runner.run(
            GarminActivityAdapter(client),
            failing_handler,
            SyncOptions(retry=RetryPolicy(max_attempts=1)),
        )

        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual(2, report.imported_count)
        self.assertEqual(_cursor(t2, "garmin-2"), self._checkpoint())

    def test_resume_after_partial_failure_starts_from_checkpoint(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        t3 = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        a2 = _make_activity("garmin-2", t2)
        a3 = _make_activity("garmin-3", t3)

        def failing_handler(connection: sqlite3.Connection, record) -> SyncDisposition:
            if record.external_id == "garmin-3":
                raise SyncError("bad_payload", "rejected")
            return garmin_handler(connection, record)

        first_client = FakeGarminClient(
            pages=[GarminActivityPage((a1, a2, a3), done=True)],
            calls=[],
        )
        self.runner.run(
            GarminActivityAdapter(first_client),
            failing_handler,
            SyncOptions(retry=RetryPolicy(max_attempts=1)),
        )
        checkpoint_after_first = self._checkpoint()

        second_client = FakeGarminClient(
            pages=[GarminActivityPage((a3,), done=True)],
            calls=[],
        )
        second_report = self.runner.run(GarminActivityAdapter(second_client), garmin_handler)

        self.assertEqual(checkpoint_after_first, second_report.cursor_start)
        self.assertEqual(1, second_report.imported_count)
        self.assertEqual(3, self._activity_count())

    def test_dry_run_does_not_write_activities_or_advance_checkpoint(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        a2 = _make_activity("garmin-2", t2)
        client = FakeGarminClient(
            pages=[GarminActivityPage((a1, a2), done=True)],
            calls=[],
        )

        report = self.runner.run(
            GarminActivityAdapter(client),
            garmin_handler,
            SyncOptions(dry_run=True),
        )

        self.assertEqual(2, report.imported_count)
        self.assertTrue(report.dry_run)
        self.assertEqual(0, self._activity_count())
        self.assertIsNone(self._checkpoint())

    def test_multi_page_fetch_passes_cursor_from_last_record_of_previous_page(self) -> None:
        t1 = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        t3 = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
        a1 = _make_activity("garmin-1", t1)
        a2 = _make_activity("garmin-2", t2)
        a3 = _make_activity("garmin-3", t3)
        client = FakeGarminClient(
            pages=[
                GarminActivityPage((a1,), done=False),
                GarminActivityPage((a2,), done=False),
                GarminActivityPage((a3,), done=True),
            ],
            calls=[],
        )

        self.runner.run(GarminActivityAdapter(client), garmin_handler, SyncOptions(batch_size=1))

        cursors = [call[0] for call in client.calls]
        self.assertEqual(
            [None, _cursor(t1, "garmin-1"), _cursor(t2, "garmin-2")],
            cursors,
        )


if __name__ == "__main__":
    unittest.main()
