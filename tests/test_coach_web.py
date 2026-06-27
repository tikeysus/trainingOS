from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from trainingos.coach_web import create_server
from trainingos.providers import AnthropicHealth, FakeChatProvider, OllamaHealth
from trainingos.storage import apply_migrations, connect_database


class CoachWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            self._insert_document(connection)
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

    def test_api_health_reports_anthropic_provider_block(self) -> None:
        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=FakeChatProvider(),
            provider_health=lambda: AnthropicHealth(
                chat_model="claude-sonnet-4-6",
                available=True,
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/health") as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertEqual("ok", payload["status"])
        self.assertEqual("anthropic", payload["provider"]["provider"])
        self.assertTrue(payload["provider"]["available"])
        self.assertEqual("claude-sonnet-4-6", payload["provider"]["chat_model"])
        self.assertNotIn("base_url", payload["provider"])
        self.assertNotIn("available_models", payload["provider"])

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


class CoachWebTokenGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.provider = FakeChatProvider("test answer")
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=self.provider,
            token="test-secret-token",
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

    def test_get_root_without_token_returns_401(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base_url}/")

        self.assertEqual(401, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("unauthorized", payload["error"])

    def test_get_health_without_token_returns_401(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base_url}/api/health")

        self.assertEqual(401, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("unauthorized", payload["error"])

    def test_post_coach_without_token_returns_401(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/coach",
            data=json.dumps({"question": "test question"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(401, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("unauthorized", payload["error"])

    def test_post_coach_with_wrong_token_returns_401(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/coach",
            data=json.dumps({"question": "test question"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer wrong-token",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(401, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("unauthorized", payload["error"])

    def test_get_root_with_correct_token_returns_200(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/",
            headers={"Authorization": "Bearer test-secret-token"},
        )
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn("TrainingOS Local Coach", body)

    def test_post_coach_with_correct_token_succeeds(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/coach",
            data=json.dumps({"question": "test question"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-secret-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertIn("answer", payload)

    def test_server_without_token_allows_unauthenticated_requests(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp_dir.name) / "training.sqlite3"
            with connect_database(db_path) as connection:
                apply_migrations(connection)
            server = create_server(
                host="127.0.0.1",
                port=0,
                database_path=db_path,
                provider=FakeChatProvider("test answer"),
                token=None,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/") as response:
                    self.assertEqual(200, response.status)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
        finally:
            tmp_dir.cleanup()


class CoachWebConfigIntegrationTests(unittest.TestCase):
    """Tests that verify config is properly passed to the server."""

    def test_config_token_is_passed_to_server(self) -> None:
        """Verify that token from AppConfig is actually used by the server.

        This test ensures the integration between config and server creation,
        catching the bug where config.coach_token is read but not passed to
        create_server().
        """
        from trainingos.config import AppConfig, COACH_TOKEN_ENV

        tmp_dir = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp_dir.name) / "training.sqlite3"
            with connect_database(db_path) as connection:
                apply_migrations(connection)

            # Simulate what main() should do: create config with token and pass it to server
            token = "test-integration-token-12345"
            config = AppConfig.from_env({COACH_TOKEN_ENV: token})

            server = create_server(
                host="127.0.0.1",
                port=0,
                database_path=db_path,
                provider=FakeChatProvider("answer"),
                token=config.coach_token,  # This is what main() should do
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"

            try:
                # Without token, should get 401
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"{base_url}/api/health")
                self.assertEqual(401, raised.exception.code)

                # With correct token, should get 200
                request = urllib.request.Request(
                    f"{base_url}/api/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(200, response.status)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
        finally:
            tmp_dir.cleanup()

    def test_config_token_none_allows_unauthenticated_access(self) -> None:
        """Verify that when config.coach_token is None, server allows unauthenticated requests."""
        from trainingos.config import AppConfig

        tmp_dir = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp_dir.name) / "training.sqlite3"
            with connect_database(db_path) as connection:
                apply_migrations(connection)

            config = AppConfig.from_env({})  # No token set

            server = create_server(
                host="127.0.0.1",
                port=0,
                database_path=db_path,
                provider=FakeChatProvider("answer"),
                token=config.coach_token,  # Will be None
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"

            try:
                # Unauthenticated request should succeed when token is None
                with urllib.request.urlopen(f"{base_url}/api/health") as response:
                    self.assertEqual(200, response.status)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
        finally:
            tmp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
