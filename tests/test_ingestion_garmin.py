from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import UTC, datetime

from trainingos.ingestion import (
    GarminActivityAdapter,
    GarminActivityPage,
    GarminActivitySummary,
)


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


if __name__ == "__main__":
    unittest.main()
