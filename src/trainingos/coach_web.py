"""Local browser UI and JSON API for the TrainingOS coach."""

from __future__ import annotations

import argparse
import html as _html_module
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from trainingos.config import AppConfig
from trainingos.domain import ContextNote, Provenance, ProvenanceKind, RecordMetadata
from trainingos.normalization import NormalizationStore
from trainingos.notes import NOTE_KIND_TYPES, NOTE_TYPE_KINDS, NOTE_TYPES, _parse_iso_date
from trainingos.presentation import CoachAnswer, CoachService, DEFAULT_EVIDENCE_LIMIT
from trainingos.providers import ChatProvider, OllamaChatProvider, OllamaHealth, check_ollama_health
from trainingos.storage import connect_database

MAX_REQUEST_BYTES = 65536


def create_coach_provider(config: AppConfig) -> OllamaChatProvider:
    return OllamaChatProvider(
        base_url=config.ollama_base_url,
        model=config.ollama_chat_model,
        timeout_seconds=config.ai_timeout_seconds,
    )


def create_server(
    *,
    host: str,
    port: int,
    database_path: Path,
    provider: ChatProvider,
    provider_health: Callable[[], OllamaHealth] | None = None,
) -> HTTPServer:
    class TrainingOSCoachHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                try:
                    self._send_json(HTTPStatus.OK, _health_payload(database_path, provider_health))
                except Exception:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error"})
                return
            if parsed.path == "/api/notes":
                query = parse_qs(parsed.query)
                type_param = query.get("type", [None])[0]
                since_param = query.get("since", [None])[0]
                filters: list[str] = []
                params: list[object] = []
                if type_param is not None and type_param in NOTE_TYPE_KINDS:
                    filters.append("note.note_kind = ?")
                    params.append(NOTE_TYPE_KINDS[type_param].value)
                if since_param is not None:
                    try:
                        since_dt = _parse_iso_date(since_param)
                    except ValueError as error:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    filters.append("date(note.occurred_at) >= ?")
                    params.append(since_dt.date().isoformat())
                where = ("WHERE " + " AND ".join(filters)) if filters else ""
                with connect_database(database_path) as connection:
                    rows = connection.execute(
                        f"""
                        SELECT note.record_id, note.occurred_at,
                               note.note_kind, note.note_text
                        FROM context_notes AS note
                        JOIN records AS record ON record.record_id = note.record_id
                        {where}
                        ORDER BY note.occurred_at DESC
                        """,
                        tuple(params),
                    ).fetchall()
                notes = [
                    {
                        "note_id": row["record_id"],
                        "type": NOTE_KIND_TYPES.get(row["note_kind"], row["note_kind"]),
                        "body": _html_module.escape(row["note_text"]),
                        "date": row["occurred_at"][:10],
                    }
                    for row in rows
                ]
                self._send_json(HTTPStatus.OK, notes)  # type: ignore[arg-type]
                return
            if parsed.path not in {"/", "/index.html"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            embedded = parse_qs(parsed.query).get("embed") == ["1"]
            body = _chat_page(embedded=embedded).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path == "/api/notes":
                try:
                    payload = self._read_json()
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                try:
                    note_type = payload.get("type")
                    if not isinstance(note_type, str) or note_type not in NOTE_TYPE_KINDS:
                        raise ValueError(
                            f"type must be one of: {', '.join(NOTE_TYPES)}"
                        )
                    body = payload.get("body")
                    if not isinstance(body, str) or not body.strip():
                        raise ValueError("body must be a non-blank string")
                    date_raw = payload.get("date")
                    if date_raw is not None:
                        try:
                            occurred_at = datetime.strptime(date_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                        except (ValueError, TypeError):
                            raise ValueError(
                                f"date must be in YYYY-MM-DD format, got: {date_raw!r}"
                            )
                    else:
                        today = datetime.now(UTC).date()
                        occurred_at = datetime(today.year, today.month, today.day, tzinfo=UTC)
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                record_id = str(uuid.uuid4())
                now = datetime.now(UTC)
                metadata = RecordMetadata(
                    record_id=record_id,
                    timezone="UTC",
                    created_at=now,
                    updated_at=now,
                    provenance=Provenance(ProvenanceKind.USER_ENTERED),
                )
                note = ContextNote(
                    metadata=metadata,
                    occurred_at=occurred_at,
                    kind=NOTE_TYPE_KINDS[note_type],
                    text=body.strip(),
                    linked_record_ids=(),
                )
                with connect_database(database_path) as connection:
                    NormalizationStore(connection).upsert_context_note(note)
                    connection.commit()
                self._send_json(HTTPStatus.CREATED, {"note_id": record_id})
                return
            if self.path != "/api/coach":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                payload = self._read_json()
                question = _required_text(payload.get("question"), "question")
                evidence_limit = _optional_evidence_limit(payload.get("evidence_limit"))
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            try:
                with connect_database(database_path) as connection:
                    service = CoachService(
                        connection,
                        provider,
                        evidence_limit=evidence_limit or DEFAULT_EVIDENCE_LIMIT,
                    )
                    answer = service.answer(question)
            except Exception:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "coach service unavailable"},
                )
                return
            self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                raise ValueError("Content-Length header is required")
            try:
                length = int(content_length)
            except ValueError as error:
                raise ValueError("Content-Length header must be an integer") from error
            if length <= 0:
                raise ValueError("request body must not be empty")
            if length > MAX_REQUEST_BYTES:
                raise ValueError("request body is too large")
            raw_body = self.rfile.read(length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("request body must be valid JSON") from error
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return HTTPServer((host, port), TrainingOSCoachHandler)


def coach_answer_to_json(answer: CoachAnswer) -> dict[str, Any]:
    return {
        "answer": answer.answer,
        "evidence": [asdict(reference) for reference in answer.evidence],
        "evidence_counts": answer.evidence_counts,
        "caveats": list(answer.caveats),
        "provider_metadata": (
            asdict(answer.provider_metadata) if answer.provider_metadata else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Host to bind; defaults to config/env")
    parser.add_argument("--port", type=int, help="Port to bind; defaults to config/env")
    parser.add_argument("--database", type=Path, help="SQLite database path")
    args = parser.parse_args()

    config = AppConfig.from_env()
    host = args.host or config.coach_host
    port = args.port or config.coach_port
    database_path = args.database or config.database_path
    server = create_server(
        host=host,
        port=port,
        database_path=database_path,
        provider=create_coach_provider(config),
        provider_health=lambda: check_ollama_health(
            base_url=config.ollama_base_url,
            chat_model=config.ollama_chat_model,
            timeout_seconds=min(config.ai_timeout_seconds, 5.0),
        ),
    )
    try:
        print(f"TrainingOS coach UI listening at http://{host}:{port}")
        server.serve_forever()
    finally:
        server.server_close()


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value.strip()


def _optional_evidence_limit(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("evidence_limit must be a positive integer")
    if value <= 0:
        raise ValueError("evidence_limit must be a positive integer")
    return value


def _health_payload(
    database_path: Path,
    provider_health: Callable[[], OllamaHealth] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "database": {
            "retrieval_documents": 0,
        },
        "provider": {"available": None},
    }
    with connect_database(database_path) as connection:
        payload["database"]["retrieval_documents"] = connection.execute(
            """
            SELECT COUNT(*)
            FROM retrieval_documents
            WHERE stale_reason IS NULL
            """
        ).fetchone()[0]
    if provider_health is not None:
        health = provider_health()
        payload["provider"] = {
            "provider": "ollama",
            "base_url": health.base_url,
            "chat_model": health.chat_model,
            "available": health.available,
            "available_models": list(health.available_models),
            "error": health.error,
        }
        if not health.available:
            payload["status"] = "degraded"
    return payload


def _chat_page(*, embedded: bool = False) -> str:
    body_class = ' class="embedded"' if embedded else ""
    heading = "" if embedded else "    <h1>TrainingOS Local Coach</h1>\n"
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TrainingOS Local Coach</title>
  <style>
    :root {
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
      color: #1f2933;
      background: #f7f8fa;
    }
    body {
      margin: 0;
    }
    main {
      max-width: 960px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }
    body.embedded main {
      max-width: none;
      padding: 12px;
    }
    h1 {
      font-size: 28px;
      margin: 0 0 16px;
      font-weight: 650;
    }
    form {
      display: grid;
      gap: 12px;
      margin-bottom: 18px;
    }
    textarea {
      min-height: 120px;
      resize: vertical;
      border: 1px solid #c5ccd6;
      border-radius: 6px;
      padding: 12px;
      font: inherit;
      background: #fff;
    }
    body.embedded textarea {
      min-height: 88px;
    }
    button {
      width: fit-content;
      border: 0;
      border-radius: 6px;
      padding: 10px 16px;
      background: #2563eb;
      color: #fff;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled {
      opacity: .6;
      cursor: wait;
    }
    section {
      background: #fff;
      border: 1px solid #d8dde6;
      border-radius: 8px;
      padding: 16px;
      margin-top: 14px;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
      font: inherit;
    }
    .meta {
      color: #596579;
      font-size: 14px;
    }
    .error {
      color: #b42318;
    }
  </style>
</head>
<body__BODY_CLASS__>
  <main>
__HEADING__
    <form id="coach-form">
      <textarea id="question" name="question" required
        placeholder="Ask about race readiness, recent training, recovery, or a block comparison."></textarea>
      <button id="submit" type="submit">Ask coach</button>
    </form>
    <section id="answer" hidden>
      <pre id="answer-text"></pre>
    </section>
    <section id="scope" hidden>
      <div class="meta" id="scope-text"></div>
    </section>
    <section id="notes-panel">
      <h2>Add a note</h2>
      <form id="notes-form">
        <select id="note-type" name="type">
          <option value="illness">illness</option>
          <option value="injury">injury</option>
          <option value="travel">travel</option>
          <option value="stress">stress</option>
          <option value="note">note</option>
        </select>
        <textarea id="note-body" name="body" placeholder="Describe what happened..." required></textarea>
        <input type="date" id="note-date" name="date">
        <button type="submit">Save note</button>
      </form>
      <ul id="notes-list"></ul>
    </section>
  </main>
  <script>
    const form = document.getElementById("coach-form");
    const question = document.getElementById("question");
    const submit = document.getElementById("submit");
    const answer = document.getElementById("answer");
    const answerText = document.getElementById("answer-text");
    const scope = document.getElementById("scope");
    const scopeText = document.getElementById("scope-text");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      answer.hidden = false;
      scope.hidden = true;
      answerText.className = "";
      answerText.textContent = "Thinking with local TrainingOS evidence...";
      try {
        const response = await fetch("/api/coach", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({question: question.value})
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "coach request failed");
        }
        answerText.textContent = payload.answer;
        const provider = payload.provider_metadata
          ? `${payload.provider_metadata.provider}/${payload.provider_metadata.model}`
          : "no provider call";
        const evidenceCount = payload.evidence.length;
        const caveats = payload.caveats.length ? payload.caveats.join("; ") : "none";
        scope.hidden = false;
        scopeText.textContent =
          `Evidence: ${evidenceCount} document(s). Provider: ${provider}. Caveats: ${caveats}.`;
      } catch (error) {
        answerText.className = "error";
        answerText.textContent = error.message;
      } finally {
        submit.disabled = false;
      }
    });
  </script>
</body>
</html>
""".replace("__BODY_CLASS__", body_class).replace("__HEADING__", heading.rstrip())


if __name__ == "__main__":
    main()
