"""Tests for the notes panel web API in the coach web server."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from trainingos.coach_web import create_server
from trainingos.domain import (
    Activity,
    ActivityType,
    Measurement,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.providers import FakeChatProvider
from trainingos.storage import apply_migrations, connect_database

_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
_TIMEZONE = "America/Toronto"


def _metadata(record_id: str) -> RecordMetadata:
    return RecordMetadata(
        record_id=record_id,
        timezone=_TIMEZONE,
        created_at=_NOW,
        updated_at=_NOW,
        provenance=Provenance(ProvenanceKind.USER_ENTERED),
    )


class NotesWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tmpdir.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.provider = FakeChatProvider("Stub coach answer.")
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=self.provider,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmpdir.cleanup()

    # ---- A. Happy Path -------------------------------------------------------

    def test_post_api_notes_persists_note_and_returns_201(self) -> None:
        payload = self._post_notes(
            {"kind": "stress", "body": "Pre-race nerves", "date": "2026-07-01"}
        )

        self.assertIn("record_id", payload)
        self.assertIsInstance(payload["record_id"], str)
        self.assertTrue(payload["record_id"])
        record_id = payload["record_id"]
        with connect_database(self.database_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM context_notes WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        self.assertIsNotNone(row)

    def test_post_api_notes_without_date_defaults_to_today(self) -> None:
        payload = self._post_notes({"kind": "note", "body": "Rest day today"})

        record_id = payload["record_id"]
        with connect_database(self.database_path) as conn:
            row = conn.execute(
                "SELECT occurred_at FROM context_notes WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        occurred_date = datetime.fromisoformat(row["occurred_at"]).date().isoformat()
        today = datetime.now(UTC).date().isoformat()
        self.assertEqual(today, occurred_date)

    def test_post_api_notes_with_activity_id_creates_link(self) -> None:
        with connect_database(self.database_path) as conn:
            NormalizationStore(conn).upsert_activity(
                Activity(
                    metadata=_metadata("act-web-1"),
                    activity_type=ActivityType.RUN,
                    started_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
                    duration=Measurement(3600, Unit.SECOND),
                    distance=Measurement(10000, Unit.METRE),
                    title="Morning run",
                )
            )
            conn.commit()

        payload = self._post_notes(
            {
                "kind": "injury",
                "body": "Shin pain after this run",
                "date": "2026-07-01",
                "activity_id": "act-web-1",
            }
        )

        record_id = payload["record_id"]
        with connect_database(self.database_path) as conn:
            link = conn.execute(
                "SELECT linked_record_id FROM context_note_links WHERE note_id = ?",
                (record_id,),
            ).fetchone()
        self.assertIsNotNone(link)
        self.assertEqual("act-web-1", link["linked_record_id"])

    def test_get_api_notes_returns_recent_notes_newest_first(self) -> None:
        for day, body in [
            ("2026-06-01", "Oldest note"),
            ("2026-06-15", "Middle note"),
            ("2026-07-01", "Newest note"),
        ]:
            self._post_notes({"kind": "note", "body": body, "date": day})

        payload = self._get_notes()

        self.assertEqual(3, len(payload))
        self.assertEqual("Newest note", payload[0]["body"])
        self.assertEqual("Middle note", payload[1]["body"])
        self.assertEqual("Oldest note", payload[2]["body"])

    def test_get_api_notes_with_type_filter(self) -> None:
        self._post_notes({"kind": "injury", "body": "Knee ache", "date": "2026-07-01"})
        self._post_notes({"kind": "stress", "body": "Work pressure", "date": "2026-07-02"})

        payload = self._get_notes(params={"type": "injury"})

        self.assertEqual(1, len(payload))
        self.assertIn("kind", payload[0])
        self.assertEqual("injury", payload[0]["kind"])

    def test_get_api_notes_with_since_filter(self) -> None:
        self._post_notes({"kind": "note", "body": "Old note", "date": "2026-05-01"})
        self._post_notes({"kind": "note", "body": "New note", "date": "2026-07-01"})

        payload = self._get_notes(params={"since": "2026-06-01"})

        self.assertEqual(1, len(payload))
        self.assertEqual("New note", payload[0]["body"])

    def test_delete_api_notes_removes_note(self) -> None:
        created = self._post_notes({"kind": "travel", "body": "Paris trip", "date": "2026-07-01"})
        record_id = created["record_id"]

        self._delete_note(record_id)

        with connect_database(self.database_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM context_notes WHERE record_id = ?", (record_id,)
            ).fetchone()
        self.assertIsNone(row)

    # ---- B / C. Boundary & Equivalence ----------------------------------------

    def test_get_api_notes_with_type_and_since_combined(self) -> None:
        self._post_notes({"kind": "stress", "body": "Old stress", "date": "2026-05-01"})
        self._post_notes({"kind": "stress", "body": "Recent stress", "date": "2026-07-01"})
        self._post_notes({"kind": "injury", "body": "Recent injury", "date": "2026-07-01"})

        payload = self._get_notes(params={"type": "stress", "since": "2026-06-15"})

        self.assertEqual(1, len(payload))
        self.assertEqual("Recent stress", payload[0]["body"])

    def test_get_api_notes_returns_empty_list_when_no_notes(self) -> None:
        payload = self._get_notes()

        self.assertEqual([], payload)

    # ---- D. Edge Cases -------------------------------------------------------

    def test_delete_api_notes_with_unknown_id_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._delete_note("does-not-exist")

        self.assertEqual(404, raised.exception.code)

    # ---- E. Error / Negative Tests -------------------------------------------

    def test_post_api_notes_with_blank_body_returns_400(self) -> None:
        error = self._post_notes_error({"kind": "note", "body": "   "})

        self.assertIn("error", error)

    def test_post_api_notes_with_invalid_kind_returns_400(self) -> None:
        error = self._post_notes_error({"kind": "snack", "body": "Post-run snack"})

        self.assertIn("error", error)

    def test_post_api_notes_missing_body_field_returns_400(self) -> None:
        error = self._post_notes_error({"kind": "note"})

        self.assertIn("error", error)

    def test_post_api_notes_missing_kind_field_returns_400(self) -> None:
        error = self._post_notes_error({"body": "No kind provided"})

        self.assertIn("error", error)

    def test_post_api_notes_with_invalid_date_format_returns_400(self) -> None:
        error = self._post_notes_error(
            {"kind": "note", "body": "Bad date", "date": "07/01/2026"}
        )

        self.assertIn("error", error)

    def test_post_api_notes_with_nonexistent_activity_id_returns_400(self) -> None:
        error = self._post_notes_error(
            {
                "kind": "injury",
                "body": "Pain after mystery run",
                "activity_id": "act-nonexistent",
            }
        )

        self.assertIn("error", error)

    # ---- F. UI Markup --------------------------------------------------------

    def test_get_index_page_includes_notes_panel_markup(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn('id="notes-form"', body)
        self.assertIn("<select", body)
        self.assertIn("illness", body)
        self.assertIn("injury", body)
        self.assertIn("travel", body)
        self.assertIn("stress", body)

    # ---- Helpers -------------------------------------------------------------

    def _post_notes(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(201, response.status)
            return json.loads(response.read().decode("utf-8"))

    def _post_notes_error(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(400, raised.exception.code)
        return json.loads(raised.exception.read().decode("utf-8"))

    def _get_notes(self, params: dict | None = None) -> list:
        url = f"{self.base_url}/api/notes"
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"
        with urllib.request.urlopen(url) as response:
            self.assertEqual(200, response.status)
            return json.loads(response.read().decode("utf-8"))

    def _delete_note(self, record_id: str) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes/{record_id}",
            method="DELETE",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(204, response.status)


if __name__ == "__main__":
    unittest.main()
