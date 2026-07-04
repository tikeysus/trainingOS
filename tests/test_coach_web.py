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


class CoachWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            self._insert_document(connection)
            self._insert_activity(connection)
        self.provider = FakeChatProvider("Local coach answer from API.")
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=self.provider,
            provider_health=lambda: OllamaHealth(
                base_url="http://localhost:11434",
                chat_model="llama3.2",
                available=False,
                error="local Ollama service is not reachable; start it with `ollama serve`",
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

    def test_serves_chat_page(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn("TrainingOS Local Coach", body)
        self.assertIn("/api/coach", body)

    def test_serves_embedded_chat_page(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/?embed=1") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn('body class="embedded"', body)
        self.assertIn("/api/coach", body)
        self.assertIn('textarea id="question"', body)
        self.assertNotIn("<h1>TrainingOS Local Coach</h1>", body)

    def test_api_returns_serialized_coach_answer(self) -> None:
        payload = self._post_json(
            "/api/coach",
            {"question": "How was my weekly distance?", "evidence_limit": 1},
        )

        self.assertEqual("Local coach answer from API.", payload["answer"])
        self.assertEqual({"week": 1}, payload["evidence_counts"])
        self.assertEqual("doc-week-1", payload["evidence"][0]["document_id"])
        self.assertEqual("fake", payload["provider_metadata"]["provider"])
        self.assertEqual(1, len(self.provider.requests))

    def test_api_health_reports_database_and_provider_status(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/api/health") as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertEqual("degraded", payload["status"])
        self.assertEqual(1, payload["database"]["retrieval_documents"])
        self.assertFalse(payload["provider"]["available"])
        self.assertEqual("ollama", payload["provider"]["provider"])
        self.assertIn("ollama serve", payload["provider"]["error"])

    def test_api_validates_question_and_evidence_limit(self) -> None:
        blank = self._post_json_error("/api/coach", {"question": " "})
        limit = self._post_json_error(
            "/api/coach",
            {"question": "weekly distance", "evidence_limit": 0},
        )

        self.assertIn("question must be a non-blank string", blank["error"])
        self.assertIn("evidence_limit must be a positive integer", limit["error"])

    def test_api_rejects_malformed_json(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/coach",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(400, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("valid JSON", payload["error"])

    def test_api_web_question_does_not_call_provider(self) -> None:
        payload = self._post_json(
            "/api/coach",
            {"question": "What are the latest online taper articles?"},
        )

        self.assertIn("only use local TrainingOS evidence", payload["answer"])
        self.assertEqual([], self.provider.requests)

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
        self,
        path: str,
        payload: dict[str, object],
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

    def _insert_document(self, connection) -> None:
        connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind, method_name, method_version
            )
            VALUES ('week-1', 'week', 'America/Toronto',
                    '2026-11-09T12:00:00+00:00',
                    '2026-11-09T12:00:00+00:00',
                    'computed', 'test_fixture', '1.0.0')
            """
        )
        connection.execute(
            """
            INSERT INTO retrieval_documents (
                document_id, document_type, source_record_id, source_updated_at,
                title, body, metadata_json, evidence_json, caveats_json,
                document_version, generated_at, stale_reason
            )
            VALUES ('doc-week-1', 'week', 'week-1',
                    '2026-11-09T12:00:00+00:00',
                    'Week 2026-11-02',
                    'Weekly distance evidence: 64 km.',
                    '{}', '["week-1"]', '[]',
                    '1.0.0', '2026-11-09T12:00:00+00:00', NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO retrieval_document_fts (document_id, title, body)
            VALUES ('doc-week-1', 'Week 2026-11-02',
                    'Weekly distance evidence: 64 km.')
            """
        )
        connection.commit()

    def _insert_activity(self, connection) -> None:
        connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at, provenance_kind
            ) VALUES ('activity-1', 'activity', 'America/Toronto',
                      '2026-07-01T07:00:00+00:00', '2026-07-01T07:00:00+00:00',
                      'user_entered')
            """
        )
        connection.execute(
            """
            INSERT INTO activities (
                record_id, activity_type, started_at, duration_seconds, distance_metres
            ) VALUES ('activity-1', 'run', '2026-07-01T05:30:00+00:00', 3600, 10000)
            """
        )
        connection.commit()


class NotesApiTests(unittest.TestCase):
    """Tests for POST /api/notes, GET /api/notes, and DELETE /api/notes/<id>."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            self._insert_activity(connection)
        self.provider = FakeChatProvider("coach answer")
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
        self.temporary_directory.cleanup()

    # -------------------------------------------------------------------------
    # Notes panel visible in page HTML
    # -------------------------------------------------------------------------

    def test_chat_page_includes_notes_panel(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            body = response.read().decode("utf-8")

        self.assertIn("/api/notes", body)
        self.assertIn("note_type", body)
        self.assertIn("note_body", body)

    # -------------------------------------------------------------------------
    # A. Happy Path
    # -------------------------------------------------------------------------

    def test_post_notes_creates_illness_note(self) -> None:
        payload = self._post_json(
            "/api/notes",
            {"type": "illness", "body": "Flu symptoms all day", "date": "2026-07-01"},
        )

        self.assertIn("record_id", payload)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT note_kind, note_text FROM context_notes"
            ).fetchone()
        self.assertEqual("illness", row["note_kind"])
        self.assertEqual("Flu symptoms all day", row["note_text"])

    def test_post_notes_defaults_date_to_today_when_omitted(self) -> None:
        payload = self._post_json(
            "/api/notes",
            {"type": "stress", "body": "Pre-race anxiety"},
        )

        self.assertIn("record_id", payload)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT occurred_at FROM context_notes"
            ).fetchone()
        self.assertIsNotNone(row["occurred_at"])

    def test_post_notes_with_activity_link_creates_link_row(self) -> None:
        payload = self._post_json(
            "/api/notes",
            {"type": "note", "body": "Felt great on the hills", "activity_id": "activity-1"},
        )

        self.assertIn("record_id", payload)
        with connect_database(self.database_path) as connection:
            links = connection.execute(
                "SELECT linked_record_id FROM context_note_links"
            ).fetchall()
        self.assertEqual(["activity-1"], [r["linked_record_id"] for r in links])

    def test_get_notes_returns_recent_notes_list(self) -> None:
        self._post_json("/api/notes", {"type": "illness", "body": "Cold", "date": "2026-06-01"})
        self._post_json("/api/notes", {"type": "injury", "body": "Shin splint", "date": "2026-07-01"})

        payload = self._get_json("/api/notes")

        self.assertIsInstance(payload, list)
        self.assertEqual(2, len(payload))
        for note in payload:
            self.assertIn("record_id", note)
            self.assertIn("type", note)
            self.assertIn("body", note)
            self.assertIn("date", note)

    def test_get_notes_returns_reverse_chronological_order(self) -> None:
        self._post_json("/api/notes", {"type": "note", "body": "Older", "date": "2026-05-01"})
        self._post_json("/api/notes", {"type": "note", "body": "Newer", "date": "2026-07-01"})

        payload = self._get_json("/api/notes")

        self.assertEqual("Newer", payload[0]["body"])
        self.assertEqual("Older", payload[1]["body"])

    def test_get_notes_with_type_filter(self) -> None:
        self._post_json("/api/notes", {"type": "illness", "body": "Head cold"})
        self._post_json("/api/notes", {"type": "injury", "body": "Knee pain"})

        payload = self._get_json("/api/notes?type=illness")

        bodies = [n["body"] for n in payload]
        self.assertIn("Head cold", bodies)
        self.assertNotIn("Knee pain", bodies)

    def test_get_notes_with_since_filter(self) -> None:
        self._post_json("/api/notes", {"type": "note", "body": "Old", "date": "2026-05-01"})
        self._post_json("/api/notes", {"type": "note", "body": "Recent", "date": "2026-07-01"})

        payload = self._get_json("/api/notes?since=2026-06-01")

        bodies = [n["body"] for n in payload]
        self.assertIn("Recent", bodies)
        self.assertNotIn("Old", bodies)

    def test_delete_note_removes_record(self) -> None:
        created = self._post_json("/api/notes", {"type": "stress", "body": "Burnout week"})
        record_id = created["record_id"]

        self._delete(f"/api/notes/{record_id}")

        with connect_database(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
        self.assertEqual(0, count)

    # -------------------------------------------------------------------------
    # B. Boundary Values
    # -------------------------------------------------------------------------

    def test_get_notes_empty_database_returns_empty_list(self) -> None:
        payload = self._get_json("/api/notes")

        self.assertEqual([], payload)

    def test_post_notes_body_at_max_length_succeeds(self) -> None:
        long_body = "x" * 4000
        payload = self._post_json("/api/notes", {"type": "note", "body": long_body})

        self.assertIn("record_id", payload)

    # -------------------------------------------------------------------------
    # E. Error / Negative Tests
    # -------------------------------------------------------------------------

    def test_post_notes_missing_type_returns_400(self) -> None:
        error = self._post_json_error("/api/notes", {"body": "text"})

        self.assertIn("type", error["error"])

    def test_post_notes_missing_body_returns_400(self) -> None:
        error = self._post_json_error("/api/notes", {"type": "illness"})

        self.assertIn("body", error["error"])

    def test_post_notes_blank_body_returns_400(self) -> None:
        error = self._post_json_error("/api/notes", {"type": "illness", "body": "   "})

        self.assertIn("body", error["error"])

    def test_post_notes_invalid_type_returns_400(self) -> None:
        error = self._post_json_error("/api/notes", {"type": "tiredness", "body": "Tired"})

        self.assertIn("type", error["error"])

    def test_post_notes_malformed_date_returns_400(self) -> None:
        error = self._post_json_error(
            "/api/notes",
            {"type": "illness", "body": "text", "date": "not-a-date"},
        )

        self.assertIn("date", error["error"])

    def test_post_notes_nonexistent_activity_id_returns_400(self) -> None:
        error = self._post_json_error(
            "/api/notes",
            {"type": "note", "body": "text", "activity_id": "missing-xyz"},
        )

        self.assertIn("activity_id", error["error"])

    def test_delete_nonexistent_note_returns_404(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/notes/no-such-id",
            method="DELETE",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(404, raised.exception.code)

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
        self.assertIn("valid JSON", payload["error"])

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

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

    def _get_json(self, path: str) -> object:
        with urllib.request.urlopen(f"{self.base_url}{path}") as response:
            return json.loads(response.read().decode("utf-8"))

    def _delete(self, path: str) -> int:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method="DELETE",
        )
        with urllib.request.urlopen(request) as response:
            return response.status

    def _insert_activity(self, connection) -> None:
        connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at, provenance_kind
            ) VALUES ('activity-1', 'activity', 'America/Toronto',
                      '2026-07-01T07:00:00+00:00', '2026-07-01T07:00:00+00:00',
                      'user_entered')
            """
        )
        connection.execute(
            """
            INSERT INTO activities (
                record_id, activity_type, started_at, duration_seconds, distance_metres
            ) VALUES ('activity-1', 'run', '2026-07-01T05:30:00+00:00', 3600, 10000)
            """
        )
        connection.commit()


class NoteTypeMapConsistencyTests(unittest.TestCase):
    """Verify CLI and API use identical note type mappings."""

    def test_cli_and_api_share_note_type_map_source(self) -> None:
        """Verify both modules import from a shared definition."""
        # This test will pass once we extract the mapping to a shared module
        from trainingos.note_types import NOTE_TYPE_MAP
        from trainingos.notes import NOTE_TYPE_MAP as cli_map
        from trainingos.coach_web import _NOTE_TYPE_MAP as api_map

        # Both should import the same object
        self.assertIs(cli_map, NOTE_TYPE_MAP, "CLI should import shared NOTE_TYPE_MAP")
        self.assertIs(api_map, NOTE_TYPE_MAP, "API should import shared NOTE_TYPE_MAP")


if __name__ == "__main__":
    unittest.main()
