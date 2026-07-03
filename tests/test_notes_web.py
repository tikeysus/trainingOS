from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from trainingos.coach_web import create_server
from trainingos.providers import FakeChatProvider, OllamaHealth
from trainingos.storage import apply_migrations, connect_database


class NotesWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=FakeChatProvider("Stub answer."),
            provider_health=lambda: OllamaHealth(
                base_url="http://localhost:11434",
                chat_model="llama3.2",
                available=False,
                error="not running",
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

    # ── A. Happy Path ──────────────────────────────────────────────────────────

    def test_post_notes_creates_note_and_returns_record_id(self) -> None:
        payload = self._post_json(
            "/api/notes",
            {"type": "illness", "body": "Sore throat.", "date": "2026-07-01"},
        )

        self.assertIn("record_id", payload)
        self.assertNotEqual("", payload["record_id"])

    def test_post_notes_without_date_defaults_to_today(self) -> None:
        from datetime import UTC, datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        self._post_json("/api/notes", {"type": "stress", "body": "Pre-race nerves."})

        notes = self._get_json("/api/notes")
        self.assertEqual(1, len(notes))
        self.assertIn(today, notes[0].get("date", ""))

    def test_get_notes_returns_notes_newest_first(self) -> None:
        self._post_json("/api/notes", {"type": "note", "body": "Older.", "date": "2026-06-01"})
        self._post_json("/api/notes", {"type": "note", "body": "Newer.", "date": "2026-07-01"})

        notes = self._get_json("/api/notes")

        self.assertEqual(2, len(notes))
        dates = [n["date"] for n in notes]
        self.assertGreater(dates[0], dates[1], "newest note should be first")

    def test_get_notes_type_filter_returns_only_matching_notes(self) -> None:
        self._post_json("/api/notes", {"type": "illness", "body": "Flu.", "date": "2026-07-01"})
        self._post_json("/api/notes", {"type": "stress", "body": "Tired.", "date": "2026-07-02"})

        notes = self._get_json("/api/notes?type=illness")

        self.assertEqual(1, len(notes))
        self.assertEqual("illness", notes[0]["type"])

    def test_delete_note_returns_204_and_removes_from_list(self) -> None:
        created = self._post_json(
            "/api/notes", {"type": "injury", "body": "Knee pain.", "date": "2026-07-01"}
        )
        record_id = created["record_id"]

        response = self._delete(f"/api/notes/{record_id}")

        self.assertEqual(204, response.status)
        notes = self._get_json("/api/notes")
        self.assertEqual([], notes)

    def test_coach_html_includes_notes_panel_markup(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn("illness", body)
        self.assertIn("injury", body)
        self.assertIn("travel", body)
        self.assertIn("stress", body)
        self.assertIn("/api/notes", body)

    # ── B. Boundary Values ─────────────────────────────────────────────────────

    def test_post_notes_with_single_character_body_succeeds(self) -> None:
        payload = self._post_json(
            "/api/notes", {"type": "note", "body": "x", "date": "2026-07-01"}
        )
        self.assertIn("record_id", payload)

    # ── C. Equivalence Partitioning ────────────────────────────────────────────

    def test_post_notes_accepts_all_five_valid_type_values(self) -> None:
        for note_type in ("illness", "injury", "travel", "stress", "note"):
            with self.subTest(note_type=note_type):
                payload = self._post_json(
                    "/api/notes",
                    {"type": note_type, "body": f"Test {note_type}.", "date": "2026-07-01"},
                )
                self.assertIn("record_id", payload)

    def test_post_notes_rejects_unknown_type_with_400(self) -> None:
        error = self._post_json_error(
            "/api/notes", {"type": "workout", "body": "Easy run.", "date": "2026-07-01"}
        )
        self.assertIn("error", error)

    # ── D. Edge Cases ──────────────────────────────────────────────────────────

    def test_get_notes_on_empty_database_returns_empty_array(self) -> None:
        notes = self._get_json("/api/notes")
        self.assertEqual([], notes)

    def test_delete_nonexistent_note_returns_404(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes/does-not-exist",
            method="DELETE",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(404, raised.exception.code)

    # ── E. Error / Negative Tests ──────────────────────────────────────────────

    def test_post_notes_missing_body_returns_400(self) -> None:
        error = self._post_json_error("/api/notes", {"type": "illness"})
        self.assertIn("error", error)
        self.assertIn("body", error["error"].lower())

    def test_post_notes_blank_body_returns_400(self) -> None:
        error = self._post_json_error(
            "/api/notes", {"type": "illness", "body": "  ", "date": "2026-07-01"}
        )
        self.assertIn("error", error)

    def test_post_notes_missing_type_returns_400(self) -> None:
        error = self._post_json_error("/api/notes", {"body": "Something happened."})
        self.assertIn("error", error)
        self.assertIn("type", error["error"].lower())

    def test_post_notes_malformed_json_returns_400(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(400, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("error", payload)

    def test_post_notes_invalid_date_format_returns_400(self) -> None:
        error = self._post_json_error(
            "/api/notes", {"type": "note", "body": "test", "date": "not-a-date"}
        )
        self.assertIn("error", error)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_json(self, path: str) -> object:
        with urllib.request.urlopen(f"{self.base_url}{path}") as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json_error(
        self, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
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

    def _delete(self, path: str) -> urllib.request.Request:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method="DELETE",
        )
        with urllib.request.urlopen(request) as response:
            return response


if __name__ == "__main__":
    unittest.main()
