from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from trainingos.presentation import CoachService
from trainingos.providers import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    ChatResponse,
    FakeChatProvider,
    ProviderError,
    ProviderErrorCategory,
    ProviderMetadata,
    ProviderUsage,
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

    def test_broad_recent_training_question_falls_back_to_recent_evidence(self) -> None:
        self._insert_document(
            document_id="doc-week-1",
            document_type="week",
            source_record_id="week-1",
            title="Week 2026-11-02",
            body="Week 2026-11-02 to 2026-11-08. Distance: 64.0 km.",
        )
        provider = FakeChatProvider("Recent local evidence was used.")
        service = CoachService(self.connection, provider)

        answer = service.answer("How is my recent training looking?")

        self.assertEqual("Recent local evidence was used.", answer.answer)
        self.assertEqual(("doc-week-1",), tuple(item.document_id for item in answer.evidence))
        self.assertEqual({"week": 1}, answer.evidence_counts)
        self.assertIn(
            "no exact local evidence match; used recent local training evidence",
            answer.caveats,
        )
        self.assertIn("doc-week-1", provider.requests[0].messages[1].content)

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

    def test_evidence_budget_truncation_is_disclosed(self) -> None:
        for index in range(3):
            self._insert_document(
                document_id=f"doc-week-{index}",
                document_type="week",
                source_record_id=f"week-{index}",
                title=f"Week {index}",
                body=f"Week {index} marathon weekly distance evidence.",
            )
        provider = FakeChatProvider("Used bounded evidence.")
        service = CoachService(self.connection, provider, evidence_limit=2)

        answer = service.answer("marathon weekly distance")

        self.assertEqual(2, len(answer.evidence))
        self.assertIn(
            "matching local evidence was truncated by evidence budget",
            answer.caveats,
        )
        self.assertIn(
            "More matching local documents existed but were omitted by budget.",
            provider.requests[0].messages[1].content,
        )

    def test_broad_race_readiness_question_searches_full_history_within_token_budget(self) -> None:
        self._insert_document(
            document_id="doc-week-1",
            document_type="week",
            source_record_id="week-1",
            title="Week 2026-11-02",
            body=(
                "Week 2026-11-02 to 2026-11-08. Marathon training week total 64.0 km. "
                "Long run 28 km at easy pace Sunday. Threshold interval session 10 km "
                "Tuesday. Recovery run 6 km Wednesday. Cumulative marathon build volume "
                "on track toward race goal. Computed metrics weekly_distance 64000 m "
                "method 1.0.0."
            ),
        )
        self._insert_document(
            document_id="doc-race-1",
            document_type="race",
            source_record_id="race-1",
            title="Hamilton Marathon",
            body=(
                "Hamilton Marathon race 2026-10-19. Target time 3:10:00. Finish 3:14:22. "
                "Positive split: held marathon pace to 32 km then faded slightly. "
                "Post-race recovery normal. Aerobic fitness consistent with marathon "
                "completion. Race readiness baseline established from this performance. "
                "Method 1.0.0."
            ),
        )
        self._insert_document(
            document_id="doc-block-1",
            document_type="training_block",
            source_record_id="block-1",
            title="Marathon Build Block",
            body=(
                "Marathon build training block 12 weeks. Peak week 85 km total distance. "
                "VO2max sessions twice weekly. Long run peak 32 km completed. Taper "
                "commenced week 11. Race readiness metrics show aerobic base sufficient "
                "for marathon target pace. Computed method version 1.0.0."
            ),
        )
        provider = FakeChatProvider("Race readiness assessment from local marathon evidence.")
        service = CoachService(self.connection, provider, token_budget=400)

        answer = service.answer("Am I ready for my marathon race?")

        self.assertGreater(len(answer.evidence), 0)
        self.assertGreater(len(answer.evidence_counts), 0)
        if len(answer.evidence) < 3:
            self.assertIn(
                "matching local evidence was truncated by evidence budget",
                answer.caveats,
            )
        self.assertGreater(len(provider.requests), 0)

    def test_ambiguous_question_fallback_respects_token_budget(self) -> None:
        for index in range(4):
            self._insert_document(
                document_id=f"doc-week-{index}",
                document_type="week",
                source_record_id=f"week-{index}",
                title=f"Week {index}",
                body=(
                    f"Week {index} summary. Total distance 55.0 km. Computed metrics "
                    "weekly_distance 55000 m method 1.0.0. Long run 20 km at easy effort "
                    "Sunday. Two quality sessions including tempo run and interval repeats. "
                    "Average pace 5:20 per km. Elevation gain 380 m. Recovery score "
                    "adequate. Heart rate zones normal. Provenance computed."
                ),
            )
        provider = FakeChatProvider()
        service = CoachService(self.connection, provider, token_budget=150)

        answer = service.answer("What should I focus on next?")

        self.assertIn("do not have enough local TrainingOS evidence", answer.answer)
        self.assertEqual((), answer.evidence)
        self.assertEqual({}, answer.evidence_counts)
        self.assertEqual([], provider.requests)

    def test_health_question_adds_informational_caveat(self) -> None:
        self._insert_document(
            document_id="doc-note-1",
            document_type="note",
            source_record_id="note-1",
            title="Injury note",
            body="Context note says calf tightness after strides.",
        )
        provider = FakeChatProvider("Interpretation references doc-note-1.")
        service = CoachService(self.connection, provider)

        answer = service.answer("What should I do about calf tightness?")

        self.assertIn(
            "health and injury guidance is informational, not medical diagnosis",
            answer.caveats,
        )
        self.assertIn(
            "not medical diagnosis",
            provider.requests[0].messages[0].content,
        )

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
        self.assertIn("timed out", answer.answer)
        self.assertEqual(("doc-workout-1",), tuple(item.document_id for item in answer.evidence))
        self.assertEqual({"workout": 1}, answer.evidence_counts)
        self.assertIn("local provider failure: timeout", answer.caveats)
        self.assertIsNone(answer.provider_metadata)

    def test_provider_unavailable_tells_user_claude_is_unreachable(self) -> None:
        self._insert_document(
            document_id="doc-workout-1",
            document_type="workout",
            source_record_id="workout-1",
            title="Workout 2026-11-02",
            body="Workout workout-1 is a run on 2026-11-02. Distance: 12.0 km.",
        )
        service = CoachService(self.connection, UnavailableChatProvider())

        answer = service.answer("Summarize my recent running.")

        self.assertIn("found local TrainingOS evidence", answer.answer)
        self.assertIn("Claude API is not reachable", answer.answer)
        self.assertIn("Check your internet connection", answer.answer)
        self.assertIn("local provider failure: provider_unavailable", answer.caveats)
        self.assertIsNone(answer.provider_metadata)

    def test_anthropic_provider_injects_cloud_caveat(self) -> None:
        self._insert_document(
            document_id="doc-week-1",
            document_type="week",
            source_record_id="week-1",
            title="Week 2026-11-02",
            body="Week 2026-11-02 distance: 50 km.",
        )
        service = CoachService(self.connection, AnthropicFakeChatProvider())

        answer = service.answer("How was my weekly distance?")

        self.assertIn(
            "training data was sent to Anthropic cloud for this answer",
            answer.caveats,
        )
        self.assertEqual("anthropic", answer.provider_metadata.provider)

    def test_token_budget_overflow_truncates_evidence_and_discloses_counts(self) -> None:
        base = "marathon weekly distance evidence. "
        padding = "x" * (400 - len(base))
        for index in range(5):
            self._insert_document(
                document_id=f"doc-week-{index}",
                document_type="week",
                source_record_id=f"week-{index}",
                title=f"Week {index}",
                body=base + padding,
            )
        provider = FakeChatProvider("Truncated answer.")
        service = CoachService(self.connection, provider, token_budget=150)

        answer = service.answer("marathon weekly distance")

        self.assertLess(len(answer.evidence), 5)
        self.assertIn("matching local evidence was truncated by evidence budget", answer.caveats)
        prompt = provider.requests[0].messages[1].content
        self.assertIn("More matching local documents existed but were omitted by budget.", prompt)
        self.assertIn("Omitted:", prompt)

    def test_provider_prompt_respects_token_budget_ceiling(self) -> None:
        base = "marathon weekly distance evidence. "
        body = base + "x" * (400 - len(base))
        for index in range(4):
            self._insert_document(
                document_id=f"doc-week-{index}",
                document_type="week",
                source_record_id=f"week-{index}",
                title=f"Week {index}",
                body=body,
            )
        provider = FakeChatProvider("Budget ceiling answer.")
        service = CoachService(self.connection, provider, token_budget=250)

        answer = service.answer("marathon weekly distance")

        self.assertLessEqual(len(answer.evidence), 2)
        self.assertLessEqual(len(provider.requests[0].messages[1].content) // 4, 250 + 200)

    def test_fake_provider_does_not_inject_cloud_caveat(self) -> None:
        self._insert_document(
            document_id="doc-week-1",
            document_type="week",
            source_record_id="week-1",
            title="Week 2026-11-02",
            body="Week 2026-11-02 distance: 50 km.",
        )
        service = CoachService(self.connection, FakeChatProvider("local answer"))

        answer = service.answer("How was my weekly distance?")

        self.assertNotIn(
            "training data was sent to Anthropic cloud for this answer",
            answer.caveats,
        )

    def test_missing_data_returns_insufficiency_regardless_of_token_budget(self) -> None:
        provider = FakeChatProvider()
        service = CoachService(self.connection, provider, token_budget=100000)

        answer = service.answer("How is my marathon training progressing?")

        self.assertIn("do not have enough local TrainingOS evidence", answer.answer)
        self.assertEqual((), answer.evidence)
        self.assertEqual({}, answer.evidence_counts)
        self.assertEqual(("no matching local retrieval evidence was found",), answer.caveats)
        self.assertEqual([], provider.requests)

    def test_zero_token_budget_raises_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            CoachService(self.connection, FakeChatProvider(), token_budget=0)

    def test_token_budget_single_document_exact_fit_includes_it(self) -> None:
        body = "weekly distance marathon training run data evidence for coach service unit tests"
        self.assertEqual(80, len(body))
        self._insert_document(
            document_id="doc-week-1",
            document_type="week",
            source_record_id="week-1",
            title="Week 2026-11-02",
            body=body,
        )
        provider = FakeChatProvider("Weekly distance fits token budget.")
        service = CoachService(self.connection, provider, token_budget=25)

        answer = service.answer("weekly distance marathon")

        self.assertEqual(1, len(answer.evidence))
        self.assertNotIn("matching local evidence was truncated by evidence budget", answer.caveats)

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
                _json_array(evidence),
                _json_array(caveats),
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


def _json_array(values: tuple[str, ...]) -> str:
    import json

    return json.dumps(list(values))


class AnthropicFakeChatProvider:
    """Fake that returns metadata with provider='anthropic' for caveat injection tests."""

    def __init__(self, response_text: str = "anthropic response") -> None:
        self.response_text = response_text
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            message=ChatMessage(role="assistant", content=self.response_text),
            metadata=ProviderMetadata(provider="anthropic", model="claude-sonnet-4-6", latency_seconds=0.1),
            usage=ProviderUsage(input_tokens=10, output_tokens=5),
            finish_reason="end_turn",
        )


class TimeoutChatProvider(ChatProvider):
    def complete(self, request: ChatRequest) -> ChatResponse:
        raise ProviderError(
            ProviderErrorCategory.TIMEOUT,
            "Claude API request timed out",
            provider="anthropic",
            retryable=True,
        )


class UnavailableChatProvider(ChatProvider):
    def complete(self, request: ChatRequest) -> ChatResponse:
        raise ProviderError(
            ProviderErrorCategory.PROVIDER_UNAVAILABLE,
            "Claude API is unavailable",
            provider="anthropic",
            retryable=True,
        )


if __name__ == "__main__":
    unittest.main()
