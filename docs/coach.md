# Local coach

TrainingOS coach answers are local-first. The coach retrieves compact evidence
documents from SQLite, then asks the configured local chat provider to
synthesize that evidence. It must not call Garmin, Strava, weather services,
web search, or cloud LLM providers for stored facts.

## Browser chat

Run the local coach UI with:

```sh
export TRAININGOS_DB_PATH="$PWD/var/trainingos.sqlite3"
PYTHONPATH=src python3 -m trainingos.coach_web
```

The default URL is `http://127.0.0.1:8765`. The Grafana dashboard includes an
`Ask Local Coach` link to `http://localhost:8765`.

The coach UI exposes:

- `GET /` for the browser chat page.
- `POST /api/coach` with JSON body `{"question": "..."}`.

API responses include the answer, evidence references, evidence counts,
caveats, and provider/model metadata when a provider call was made.

## Local Ollama provider

The first real provider is Ollama over its local HTTP API. TrainingOS uses
non-streaming chat requests and embedding requests through provider-neutral
interfaces, so provider payloads do not leak into retrieval or presentation
code.

Default configuration:

```sh
TRAININGOS_AI_PROVIDER=ollama
TRAININGOS_OLLAMA_BASE_URL=http://localhost:11434
TRAININGOS_OLLAMA_CHAT_MODEL=llama3.2
TRAININGOS_OLLAMA_EMBEDDING_MODEL=nomic-embed-text
TRAININGOS_AI_TIMEOUT_SECONDS=30
TRAININGOS_COACH_HOST=127.0.0.1
TRAININGOS_COACH_PORT=8765
```

The Ollama base URL must point to a local host. Cloud model endpoints, web
search, and online research are intentionally out of scope for the local coach.

## Evidence behavior

The coach uses `retrieval_documents` and SQLite FTS as its evidence source. A
coach answer returns the included document IDs, evidence counts by document
type, caveats, and provider/model metadata.

When matching evidence is missing, the coach returns a low-confidence local
data insufficiency answer without calling the chat provider. When a question
asks for latest, online, article, live, or web evidence, the coach discloses
that web research was not searched or considered.

LLMs may explain and synthesize local evidence, but numeric training metrics
must come from deterministic TrainingOS analytics and stored metric evidence.
