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
    ContextNote,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
)
from trainingos.normalization import NormalizationStore
from trainingos.providers import FakeChatProvider, OllamaHealth
from trainingos.storage import apply_migrations, connect_database


class NotesWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.provider = FakeChatProvider("Stub coach answer.")
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=self.provider,
            provider_health=lambda: OllamaHealth(
                base_url="http://localhost:11434",
                chat_model="llama3.2",
                available=False,
                error="not reachable",
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

    def test_post_note_persists_note_and_returns_record_id(self) -> None:
        response_status, payload = self._post_json_with_status(
            "/api/notes",
            {"type": "stress", "body": "Pre-race nerves, sleep disrupted", "date": "2026-07-03"},
        )

        self.assertEqual(201, response_status)
        self.assertIn("record_id", payload)
        with connect_database(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
            row = connection.execute("SELECT note_kind, note_text FROM context_notes").fetchone()
        self.assertEqual(1, count)
        self.assertEqual("stress", row["note_kind"])
        self.assertEqual("Pre-race nerves, sleep disrupted", row["note_text"])

    def test_post_note_without_date_defaults_to_today(self) -> None:
        self._post_json("/api/notes", {"type": "illness", "body": "Cold symptoms"})

        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT occurred_at FROM context_notes").fetchone()
        from datetime import date
        self.assertTrue(
            row["occurred_at"].startswith(date.today().isoformat()),
            row["occurred_at"],
        )

    def test_get_notes_returns_recent_notes_newest_first(self) -> None:
        self._seed_notes()

        with urllib.request.urlopen(f"{self.base_url}/api/notes") as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertIsInstance(payload, list)
        self.assertEqual(2, len(payload))
        self.assertGreaterEqual(payload[0]["occurred_at"], payload[1]["occurred_at"])

    def test_get_notes_response_includes_kind_and_body(self) -> None:
        self._seed_notes()

        with urllib.request.urlopen(f"{self.base_url}/api/notes") as response:
            notes = json.loads(response.read().decode("utf-8"))

        kinds = {n["kind"] for n in notes}
        bodies = {n["body"] for n in notes}
        self.assertIn("illness", kinds)
        self.assertIn("injury", kinds)
        self.assertIn("Head cold, missed easy run", bodies)
        self.assertIn("Right knee swelling", bodies)

    def test_post_note_rejects_blank_body(self) -> None:
        error = self._post_json_error("/api/notes", {"type": "illness", "body": "  "})
        self.assertIn("body", error["error"].lower())

    def test_post_note_rejects_invalid_type(self) -> None:
        error = self._post_json_error("/api/notes", {"type": "hangover", "body": "bad input"})
        self.assertTrue(
            any(t in error["error"] for t in ("illness", "injury", "travel", "stress", "note")),
            f"Expected valid types in error: {error['error']!r}",
        )

    def test_post_note_rejects_malformed_json(self) -> None:
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
        self.assertIn("valid JSON", payload["error"])

    def test_get_notes_returns_empty_list_when_no_notes_exist(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/api/notes") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(200, response.status)
        self.assertEqual([], payload)

    def test_post_note_returns_201_created_status(self) -> None:
        status, payload = self._post_json_with_status(
            "/api/notes",
            {"type": "illness", "body": "Test note"},
        )
        self.assertEqual(201, status, "POST should return 201 Created, not 200 OK")
        self.assertIn("record_id", payload)

    def test_post_note_with_query_params_still_creates_note(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes?extra=param",
            data=json.dumps({"type": "stress", "body": "Note with query params"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertIn("record_id", payload)
        with connect_database(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
        self.assertEqual(1, count, "query params on POST should not prevent note creation")

    def _post_json(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json_with_status(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post_json_error(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(400, raised.exception.code)
        return json.loads(raised.exception.read().decode("utf-8"))

    def _seed_notes(self) -> None:
        with connect_database(self.database_path) as connection:
            store = NormalizationStore(connection)
            store.upsert_context_note(
                ContextNote(
                    metadata=RecordMetadata(
                        record_id="note-illness-1",
                        timezone="America/Toronto",
                        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
                        updated_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
                        provenance=Provenance(ProvenanceKind.USER_ENTERED),
                    ),
                    occurred_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
                    kind=NoteKind.ILLNESS,
                    text="Head cold, missed easy run",
                )
            )
            store.upsert_context_note(
                ContextNote(
                    metadata=RecordMetadata(
                        record_id="note-injury-1",
                        timezone="America/Toronto",
                        created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
                        updated_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
                        provenance=Provenance(ProvenanceKind.USER_ENTERED),
                    ),
                    occurred_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
                    kind=NoteKind.INJURY,
                    text="Right knee swelling",
                )
            )
            connection.commit()


if __name__ == "__main__":
    unittest.main()
