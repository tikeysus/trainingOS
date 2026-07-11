---
name: add-ai-provider
description: Add or change an AI chat or embedding provider adapter in TrainingOS. Use for OpenAI, Anthropic, Gemini, Ollama, or local-provider integrations, shared interfaces, configuration, model metadata, timeouts, error mapping, or provider conformance tests. Do not use AI providers as sources of numeric training facts.
---

# Add AI Provider

## Workflow

1. Inspect shared chat and embedding interfaces, the provider registry, configuration, retrieval contracts, and conformance tests.
2. Extend shared interfaces only for capabilities required across providers. Keep provider SDK request, response, streaming, and error types inside the adapter.
3. Map shared requests and responses explicitly. Preserve provider/model identifiers, revision when available, usage, finish reason, latency, and correlation metadata without secrets.
4. Configure provider, endpoint, model, credentials, timeout, and retry policy outside domain code. Support local endpoints without assuming internet access.
5. Apply finite timeouts. Retry only transient, idempotent operations with bounded backoff; map authentication, rate-limit, timeout, malformed-response, and unsupported-capability failures.
6. Add shared conformance and adapter mapping tests using fakes or sanitized recorded responses, never live provider calls.
7. Verify retrieval and coaching depend only on shared domain types and locally stored evidence.

Read [references/checklist.md](references/checklist.md) before finalizing the change.

## Boundaries

- Keep chat and embedding capabilities separate.
- Do not leak provider types into ingestion, analytics, retrieval, or UI layers.
- Do not log keys, private prompts, or raw provider payloads by default.
- Use LLMs for explanation and synthesis, never numeric metrics.
