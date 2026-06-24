# Local coach

TrainingOS coach answers are local-first. The coach retrieves compact evidence
documents from SQLite, then asks a configured local chat provider to synthesize
that evidence. It must not call Garmin, Strava, weather services, web search, or
cloud LLM providers for stored facts.

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
```

The Ollama base URL must point to a local host. Cloud model endpoints, web
search, and online research are intentionally out of scope for the local coach.

Ollama API reference:
https://github.com/ollama/ollama/blob/main/docs/api.md

Start Ollama before asking the local coach:

```sh
ollama serve
ollama pull llama3.2
```

Check the coach runtime status with:

```sh
curl http://localhost:8765/api/health
```

If the provider status is degraded, confirm `ollama list` works and that
`TRAININGOS_OLLAMA_CHAT_MODEL` matches an installed local model.

## Evidence behavior

The coach uses `retrieval_documents` and SQLite FTS as its evidence source. A
coach answer returns the included document IDs, evidence counts by document
type, caveats, and provider/model metadata.

Generate or refresh those documents from persisted local facts with:

```sh
PYTHONPATH=src python3 -m trainingos.refresh
```

When matching evidence is missing, the coach returns a low-confidence local
data insufficiency answer without calling the chat provider. When a question
asks for latest, online, article, live, or web evidence, the coach discloses
that web research was not searched or considered.

LLMs may explain and synthesize local evidence, but numeric training metrics
must come from deterministic TrainingOS analytics and stored metric evidence.
