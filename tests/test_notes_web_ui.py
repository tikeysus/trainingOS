from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from trainingos.coach_web import create_server
from trainingos.providers import FakeChatProvider, OllamaHealth
from trainingos.storage import apply_migrations, connect_database


def _seed_activity(connection, activity_id: str = "activity-web-1") -> str:
    connection.execute(
        """
        INSERT INTO sync_runs (sync_run_id, source, status, started_at, finished_at)
        VALUES ('run-web-1', 'fixture', 'completed',
                '2026-07-01T06:00:00+00:00', '2026-07-01T06:01:00+00:00')
        """
    )
    connection.execute(
        f"""
        INSERT INTO records (record_id, record_type, timezone, created_at, updated_at,
                             provenance_kind)
        VALUES ('{activity_id}', 'activity', 'America/Toronto',
                '2026-07-01T06:00:00+00:00', '2026-07-01T06:00:00+00:00',
                'imported')
        """
    )
    connection.execute(
        f"""
        INSERT INTO activities (record_id, activity_type, started_at,
                                duration_seconds, distance_metres)
        VALUES ('{activity_id}', 'run', '2026-07-01T05:30:00+00:00', 3600, 10000)
        """
    )
    connection.commit()
    return activity_id


class NotesWebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=FakeChatProvider("Test answer."),
            provider_health=lambda: OllamaHealth(
                base_url="http://localhost:11434",
                chat_model="llama3.2",
                available=False,
                error="not available in tests",
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary_directory.cleanup()

    # A. Happy Path

    def test_get_index_renders_notes_panel(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn("<form", body)
        self.assertIn("<select", body)
        self.assertIn("<textarea", body)
        # All user-facing note type options must be present
        for kind in ("illness", "injury", "travel", "stress", "note"):
            self.assertIn(kind, body, f"Missing note type option: {kind}")

    def test_post_api_notes_creates_note(self) -> None:
        payload = self._post_json(
            "/api/notes",
            {"kind": "travel", "body": "Boston trip", "date": "2026-07-01"},
        )

        self.assertIn("record_id", payload)
        with connect_database(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
        self.assertEqual(1, count)

    def test_post_api_notes_returns_201(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes",
            data=json.dumps({"kind": "stress", "body": "Deadline week", "date": "2026-07-01"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(201, response.status)

    def test_post_api_notes_defaults_date_to_today_when_omitted(self) -> None:
        self._post_json("/api/notes", {"kind": "note", "body": "No date provided"})

        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT occurred_at FROM context_notes").fetchone()
        today = date.today().isoformat()
        self.assertIn(today, row["occurred_at"])

    def test_get_api_notes_returns_recent_notes_list(self) -> None:
        for i, (note_type, body, note_date) in enumerate([
            ("illness", "Flu", "2026-06-01"),
            ("stress", "Exam week", "2026-06-15"),
            ("travel", "Race trip", "2026-07-01"),
        ]):
            self._post_json("/api/notes", {"kind": note_type, "body": body, "date": note_date})

        with urllib.request.urlopen(f"{self.base_url}/api/notes") as response:
            notes = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertIsInstance(notes, list)
        self.assertEqual(3, len(notes))
        # Newest first
        self.assertIn("2026-07-01", notes[0]["date"])
        self.assertIn("2026-06-01", notes[2]["date"])
        # Each note has required fields
        for note in notes:
            self.assertIn("record_id", note)
            self.assertIn("kind", note)
            self.assertIn("body", note)
            self.assertIn("date", note)

    def test_get_api_notes_filters_by_type(self) -> None:
        self._post_json("/api/notes", {"kind": "illness", "body": "Flu", "date": "2026-07-01"})
        self._post_json("/api/notes", {"kind": "stress", "body": "Work", "date": "2026-07-02"})

        with urllib.request.urlopen(f"{self.base_url}/api/notes?type=illness") as response:
            notes = json.loads(response.read().decode("utf-8"))

        self.assertEqual(1, len(notes))
        self.assertEqual("illness", notes[0]["kind"])

    def test_get_api_notes_filters_by_since(self) -> None:
        self._post_json("/api/notes", {"kind": "note", "body": "June note", "date": "2026-06-01"})
        self._post_json("/api/notes", {"kind": "note", "body": "July note", "date": "2026-07-01"})

        with urllib.request.urlopen(f"{self.base_url}/api/notes?since=2026-07-01") as response:
            notes = json.loads(response.read().decode("utf-8"))

        self.assertEqual(1, len(notes))
        self.assertIn("July note", notes[0]["body"])

    # D. Edge Cases

    def test_post_api_notes_body_with_special_html_characters_stored_literally(self) -> None:
        body = "<b>sick</b> & tired"
        self._post_json("/api/notes", {"kind": "illness", "body": body, "date": "2026-07-01"})

        with connect_database(self.database_path) as connection:
            stored = connection.execute(
                "SELECT note_text FROM context_notes"
            ).fetchone()["note_text"]
        self.assertEqual(body, stored)

    def test_get_api_notes_html_body_is_escaped_in_json_response(self) -> None:
        body = "<script>alert(1)</script>"
        self._post_json("/api/notes", {"kind": "note", "body": body, "date": "2026-07-01"})

        with urllib.request.urlopen(f"{self.base_url}/api/notes") as response:
            raw = response.read().decode("utf-8")

        # The raw JSON string must not contain an unescaped <script> tag
        self.assertNotIn("<script>", raw)

    # E. Error / Negative Tests

    def test_post_api_notes_rejects_blank_body(self) -> None:
        error = self._post_json_error("/api/notes", {"kind": "stress", "body": "   "})

        self.assertIn("body", error["error"].lower())

    def test_post_api_notes_rejects_unknown_type(self) -> None:
        error = self._post_json_error("/api/notes", {"kind": "mystery", "body": "?"})

        self.assertIn("kind", error["error"].lower())

    def test_post_api_notes_rejects_malformed_json(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(400, raised.exception.code)
        error = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("valid JSON", error["error"])

    def test_post_api_notes_missing_type_field_is_rejected(self) -> None:
        error = self._post_json_error("/api/notes", {"body": "Missing kind"})

        self.assertIn("kind", error["error"].lower())

    def test_post_api_notes_missing_body_field_is_rejected(self) -> None:
        error = self._post_json_error("/api/notes", {"kind": "note"})

        self.assertIn("body", error["error"].lower())

    def test_post_api_notes_invalid_date_format_rejected(self) -> None:
        error = self._post_json_error(
            "/api/notes", {"kind": "note", "body": "x", "date": "not-a-date"}
        )

        self.assertIn("date", error["error"].lower())

    # Helpers

    def _post_json(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json_error(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(400, raised.exception.code)
        return json.loads(raised.exception.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
