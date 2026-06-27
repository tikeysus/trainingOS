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

## Network exposure and privacy

By default, the coach UI binds to `127.0.0.1` (localhost only). This default
prevents unintended external network exposure of:

- Your complete running/training history
- Weekly metrics, recovery, and training-load data
- AI analysis and coaching responses
- Personal performance trends and race planning

The default localhost-only binding means the UI is only accessible from your
local machine. If you choose to expose it externally, anyone with network access
could see all your training data and get personalized coaching based on it.

### Accessing from another machine

To access the coach UI from another machine on your network:

1. **Set the opt-in flag** (explicit acknowledgment of the privacy tradeoff):
   ```sh
   TRAININGOS_COACH_ALLOW_EXTERNAL=1
   ```

2. **Configure the bind host**:
   ```sh
   TRAININGOS_COACH_HOST=0.0.0.0    # All interfaces
   # or
   TRAININGOS_COACH_HOST=192.168.1.5  # Specific IP
   ```

3. **(Strongly recommended) Add token-based authentication**:
   ```sh
   TRAININGOS_COACH_TOKEN=$(head -c32 /dev/urandom | base64)
   ```

### Complete example: External access with authentication

```sh
TRAININGOS_COACH_HOST=0.0.0.0 \
TRAININGOS_COACH_ALLOW_EXTERNAL=1 \
TRAININGOS_COACH_TOKEN=my-secret-token-12345 \
python3 -m trainingos.coach_web
```

Client requests must include the token in the Authorization header:

```sh
curl -H "Authorization: Bearer my-secret-token-12345" \
     http://remote-host:8765/api/health
```

From the browser, provide the token in the first request and it will be
remembered in the browser's runtime session.

### Security considerations

- **Token is not encryption**: Bearer tokens protect against casual access but
  should be transmitted over HTTPS in production. Local network traffic is
  generally safe, but do not expose the coach UI to the public internet without
  additional hardening (firewall rules, VPN, reverse proxy with TLS).

- **No rate limiting**: The coach service does not implement rate limiting.
  If exposed, a large number of concurrent requests could consume local
  resources or AI provider quota.

- **Local data sensitivity**: Treat the token like a password. Store it in
  environment files (never in version control) or use a secrets manager.
