from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trainingos.presentation import CoachService
from trainingos.providers import (
    ChatProvider,
    ChatRequest,
    ChatResponse,
    FakeChatProvider,
    ProviderError,
    ProviderErrorCategory,
)
from trainingos.storage import apply_migrations, connect_database


class CoachServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.connection = connect_database(
            Path(self.temporary_directory.name) / "training.sqlite3"
        )
        apply_migrations(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_answer_uses_local_retrieval_evidence_and_returns_scope(self) -> None:
        self._insert_document(
            document_id="doc-week-1",
            document_type="week",
            source_record_id="week-1",
            title="Week 2026-11-02",
            body=(
                "Week 2026-11-02 to 2026-11-08. Distance: 64.0 km. "
                "Computed metrics: weekly_distance 64000 m method 1.0.0."
            ),
            evidence=("week-1", "metric-1"),
            caveats=("recovery coverage is incomplete",),
        )
        provider = FakeChatProvider("Observed facts: 64.0 km from doc-week-1.")
        service = CoachService(self.connection, provider)

        answer = service.answer("How was my weekly distance?")

        self.assertEqual("Observed facts: 64.0 km from doc-week-1.", answer.answer)
        self.assertEqual(("doc-week-1",), tuple(item.document_id for item in answer.evidence))
        self.assertEqual({"week": 1}, answer.evidence_counts)
        self.assertIn("recovery coverage is incomplete", answer.caveats)
        self.assertEqual("fake", answer.provider_metadata.provider)
        prompt = provider.requests[0].messages[1].content
        self.assertIn("doc-week-1", prompt)
        self.assertIn("weekly_distance 64000 m method 1.0.0", prompt)
        self.assertIn("Evidence record IDs: week-1, metric-1", prompt)

    def test_empty_evidence_returns_data_insufficiency_without_provider_call(self) -> None:
        provider = FakeChatProvider()
        service = CoachService(self.connection, provider)

        answer = service.answer("How is my marathon readiness?")

        self.assertIn("do not have enough local TrainingOS evidence", answer.answer)
        self.assertEqual((), answer.evidence)
        self.assertEqual({}, answer.evidence_counts)
        self.assertEqual(("no matching local retrieval evidence was found",), answer.caveats)
        self.assertEqual([], provider.requests)

    def test_web_or_current_question_is_rejected_without_provider_call(self) -> None:
        self._insert_document(
            document_id="doc-race-1",
            document_type="race",
            source_record_id="race-1",
            title="Hamilton Marathon",
            body="Hamilton Marathon target race with 3:10 target.",
        )
        provider = FakeChatProvider()
        service = CoachService(self.connection, provider)

        answer = service.answer("What are the latest online articles about tapering?")

        self.assertIn("only use local TrainingOS evidence", answer.answer)
        self.assertIn("not searched or considered", answer.answer)
        self.assertEqual(("web research is outside the local-only coach scope",), answer.caveats)
        self.assertEqual([], provider.requests)

    def test_provider_timeout_returns_scoped_failure_answer(self) -> None:
        self._insert_document(
            document_id="doc-workout-1",
            document_type="workout",
            source_record_id="workout-1",
            title="Workout 2026-11-02",
            body="Workout workout-1 is a run on 2026-11-02. Distance: 12.0 km.",
        )
        service = CoachService(self.connection, TimeoutChatProvider())

        answer = service.answer("Summarize my recent running.")

        self.assertIn("found local TrainingOS evidence", answer.answer)
        self.assertIn("timeout", answer.answer)
        self.assertEqual(("doc-workout-1",), tuple(item.document_id for item in answer.evidence))
        self.assertEqual({"workout": 1}, answer.evidence_counts)
        self.assertIn("local provider failure: timeout", answer.caveats)
        self.assertIsNone(answer.provider_metadata)

    def _insert_document(
        self,
        *,
        document_id: str,
        document_type: str,
        source_record_id: str,
        title: str,
        body: str,
        evidence: tuple[str, ...] = (),
        caveats: tuple[str, ...] = (),
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind, method_name, method_version
            )
            VALUES (?, ?, 'America/Toronto', '2026-11-09T12:00:00+00:00',
                    '2026-11-09T12:00:00+00:00', 'computed',
                    'test_fixture', '1.0.0')
            """,
            (source_record_id, document_type),
        )
        for evidence_record_id in evidence:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO records (
                    record_id, record_type, timezone, created_at, updated_at,
                    provenance_kind, method_name, method_version
                )
                VALUES (?, 'metric_evidence', 'America/Toronto',
                        '2026-11-09T12:00:00+00:00',
                        '2026-11-09T12:00:00+00:00', 'computed',
                        'test_fixture', '1.0.0')
                """,
                (evidence_record_id,),
            )
        self.connection.execute(
            """
            INSERT INTO retrieval_documents (
                document_id, document_type, source_record_id, source_updated_at,
                title, body, metadata_json, evidence_json, caveats_json,
                document_version, generated_at, stale_reason
            )
            VALUES (?, ?, ?, '2026-11-09T12:00:00+00:00', ?, ?, '{}', ?, ?,
                    '1.0.0', '2026-11-09T12:00:00+00:00', NULL)
            """,
            (
                document_id,
                document_type,
                source_record_id,
                title,
                body,
                json.dumps(list(evidence)),
                json.dumps(list(caveats)),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO retrieval_document_fts (document_id, title, body)
            VALUES (?, ?, ?)
            """,
            (document_id, title, body),
        )
        self.connection.commit()


class TimeoutChatProvider(ChatProvider):
    def complete(self, request: ChatRequest) -> ChatResponse:
        raise ProviderError(
            ProviderErrorCategory.TIMEOUT,
            "local Ollama request timed out",
            provider="ollama",
            retryable=True,
        )


if __name__ == "__main__":
    unittest.main()
