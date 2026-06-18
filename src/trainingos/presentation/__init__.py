"""Dashboard and coaching application services."""

from __future__ import annotations

import sqlite3
import json
import re
from dataclasses import dataclass

from trainingos.providers import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    ProviderError,
    ProviderMetadata,
)
from trainingos.retrieval import (
    DOCUMENT_TYPES,
    RetrievalDocument,
    RetrievalResult,
    search_retrieval_documents,
)

DEFAULT_EVIDENCE_LIMIT = 20
QUESTION_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "am",
        "an",
        "are",
        "do",
        "for",
        "how",
        "i",
        "is",
        "me",
        "my",
        "of",
        "on",
        "should",
        "the",
        "to",
        "was",
        "what",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    document_id: str
    document_type: str
    source_record_id: str
    title: str


@dataclass(frozen=True, slots=True)
class CoachAnswer:
    answer: str
    evidence: tuple[EvidenceReference, ...]
    evidence_counts: dict[str, int]
    caveats: tuple[str, ...]
    provider_metadata: ProviderMetadata | None = None


class CoachService:
    """Answer coaching questions from local retrieval evidence only."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        chat_provider: ChatProvider,
        *,
        evidence_limit: int = DEFAULT_EVIDENCE_LIMIT,
    ) -> None:
        if evidence_limit <= 0:
            raise ValueError("evidence_limit must be positive")
        self.connection = connection
        self.chat_provider = chat_provider
        self.evidence_limit = evidence_limit

    def answer(self, question: str) -> CoachAnswer:
        _require_text(question, "question")
        if _asks_for_live_or_web_evidence(question):
            return CoachAnswer(
                answer=(
                    "I can only use local TrainingOS evidence for this coach answer. "
                    "Online articles, current race updates, and live web facts were not "
                    "searched or considered."
                ),
                evidence=(),
                evidence_counts={},
                caveats=("web research is outside the local-only coach scope",),
            )
        selection = self._select_evidence(question)
        if not selection.results:
            return CoachAnswer(
                answer=(
                    "I do not have enough local TrainingOS evidence to answer this "
                    "with confidence. Generate retrieval documents from local "
                    "activities, weekly summaries, notes, races, or training blocks "
                    "and ask again."
                ),
                evidence=(),
                evidence_counts={},
                caveats=("no matching local retrieval evidence was found",),
            )
        try:
            response = self.chat_provider.complete(
                ChatRequest(
                    messages=(
                        ChatMessage(role="system", content=_system_prompt()),
                        ChatMessage(
                            role="user",
                            content=_user_prompt(
                                question,
                                selection.results,
                                selection.truncated,
                            ),
                        ),
                    ),
                    temperature=0.2,
                )
            )
        except ProviderError as error:
            return CoachAnswer(
                answer=(
                    "I found local TrainingOS evidence for this question, but the "
                    f"{error.provider} provider could not complete the answer "
                    f"because of a {error.category.value} error. Try again with a "
                    "higher TRAININGOS_AI_TIMEOUT_SECONDS value, a smaller "
                    "evidence_limit, or a smaller local model."
                ),
                evidence=_evidence_references(selection.results),
                evidence_counts=_evidence_counts(selection.results),
                caveats=(
                    *_answer_caveats(question, selection),
                    f"local provider failure: {error.category.value}",
                ),
            )
        return CoachAnswer(
            answer=response.message.content,
            evidence=_evidence_references(selection.results),
            evidence_counts=_evidence_counts(selection.results),
            caveats=_answer_caveats(question, selection),
            provider_metadata=response.metadata,
        )

    def _select_evidence(self, question: str) -> "EvidenceSelection":
        queries = tuple(dict.fromkeys((question, _keyword_query(question))))
        for query in queries:
            selection = self._search_evidence(query)
            if selection.results:
                return selection
        if _asks_for_broad_training_context(question):
            return self._recent_evidence()
        return EvidenceSelection(results=(), truncated=False)

    def _search_evidence(self, query: str) -> "EvidenceSelection":
        if not query:
            return EvidenceSelection(results=(), truncated=False)
        try:
            results = search_retrieval_documents(
                self.connection,
                query,
                document_types=tuple(sorted(DOCUMENT_TYPES)),
                limit=self.evidence_limit + 1,
            )
        except ValueError:
            return EvidenceSelection(results=(), truncated=False)
        truncated = len(results) > self.evidence_limit
        return EvidenceSelection(
            results=results[: self.evidence_limit],
            truncated=truncated,
        )

    def _recent_evidence(self) -> "EvidenceSelection":
        rows = self.connection.execute(
            """
            SELECT *
            FROM retrieval_documents
            WHERE stale_reason IS NULL
              AND document_type IN ('week', 'workout', 'activity', 'training_block', 'race')
            ORDER BY source_updated_at DESC, document_id DESC
            LIMIT ?
            """,
            (self.evidence_limit + 1,),
        ).fetchall()
        results = tuple(
            RetrievalResult(document=_document_from_row(row), rank=0.0)
            for row in rows[: self.evidence_limit]
        )
        return EvidenceSelection(
            results=results,
            truncated=len(rows) > self.evidence_limit,
            fallback_reason=(
                "no exact local evidence match; used recent local training evidence"
                if results
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    results: tuple[RetrievalResult, ...]
    truncated: bool
    fallback_reason: str | None = None


def _system_prompt() -> str:
    return (
        "You are the TrainingOS local coach. Answer only from the supplied local "
        "SQLite retrieval evidence. Do not claim to browse the web, call Garmin, "
        "call Strava, call weather services, or use live provider facts. Distinguish "
        "observed facts, computed estimates, and interpretation. Numeric training "
        "metrics must come from the supplied evidence, not your prior knowledge. "
        "Include relevant dates, units, formula versions, caveats, uncertainty, and "
        "data gaps. For pain, illness, injury, or health questions, state that this "
        "is informational and not medical diagnosis."
    )


def _user_prompt(
    question: str,
    results: tuple[RetrievalResult, ...],
    truncated: bool,
) -> str:
    lines = [
        f"Question: {question}",
        "",
        "Evidence scope:",
        f"- Included {len(results)} local retrieval documents.",
    ]
    if truncated:
        lines.append("- More matching local documents existed but were omitted by budget.")
    lines.append("")
    lines.append("Local evidence:")
    for index, result in enumerate(results, start=1):
        document = result.document
        caveats = "; ".join(document.caveats) if document.caveats else "none"
        evidence = (
            ", ".join(document.evidence_record_ids)
            if document.evidence_record_ids
            else "none"
        )
        lines.extend(
            (
                f"[{index}] {document.document_type} {document.document_id}",
                f"Title: {document.title}",
                f"Source record: {document.source_record_id}",
                f"Source updated at: {document.source_updated_at}",
                f"Document version: {document.document_version}",
                f"Evidence record IDs: {evidence}",
                f"Caveats: {caveats}",
                f"Body: {document.body}",
                "",
            )
        )
    lines.append(
        "Answer with explicit evidence references by document ID where they support "
        "the conclusion."
    )
    return "\n".join(lines)


def _evidence_references(
    results: tuple[RetrievalResult, ...],
) -> tuple[EvidenceReference, ...]:
    return tuple(
        EvidenceReference(
            document_id=result.document.document_id,
            document_type=result.document.document_type,
            source_record_id=result.document.source_record_id,
            title=result.document.title,
        )
        for result in results
    )


def _evidence_counts(results: tuple[RetrievalResult, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.document.document_type] = counts.get(result.document.document_type, 0) + 1
    return counts


def _answer_caveats(
    question: str,
    selection: EvidenceSelection,
) -> tuple[str, ...]:
    caveats: list[str] = []
    if selection.truncated:
        caveats.append("matching local evidence was truncated by evidence budget")
    if selection.fallback_reason:
        caveats.append(selection.fallback_reason)
    for result in selection.results:
        caveats.extend(result.document.caveats)
    if _asks_health_question(question):
        caveats.append("health and injury guidance is informational, not medical diagnosis")
    return tuple(dict.fromkeys(caveats))


def _asks_for_live_or_web_evidence(question: str) -> bool:
    normalized = question.lower()
    markers = (
        "article",
        "browse",
        "current",
        "internet",
        "latest",
        "live",
        "news",
        "online",
        "research paper",
        "study",
        "today",
        "tomorrow",
        "web",
    )
    return any(marker in normalized for marker in markers)


def _asks_health_question(question: str) -> bool:
    normalized = question.lower()
    markers = ("ache", "hurt", "illness", "injury", "pain", "sick", "tightness")
    return any(marker in normalized for marker in markers)


def _asks_for_broad_training_context(question: str) -> bool:
    normalized = question.lower()
    markers = (
        "activity",
        "activities",
        "block",
        "fitness",
        "readiness",
        "recent",
        "training",
        "week",
        "workout",
        "workouts",
    )
    return any(marker in normalized for marker in markers)


def _document_from_row(row: sqlite3.Row) -> RetrievalDocument:
    return RetrievalDocument(
        document_id=row["document_id"],
        document_type=row["document_type"],
        source_record_id=row["source_record_id"],
        source_updated_at=row["source_updated_at"],
        title=row["title"],
        body=row["body"],
        metadata=json.loads(row["metadata_json"]),
        evidence_record_ids=tuple(json.loads(row["evidence_json"])),
        caveats=tuple(json.loads(row["caveats_json"])),
        document_version=row["document_version"],
        stale_reason=row["stale_reason"],
    )


def _keyword_query(question: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[\w]+", question.lower())
        if token not in QUESTION_STOPWORDS and len(token) > 1
    ]
    return " ".join(tokens)


def _require_text(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be blank")
