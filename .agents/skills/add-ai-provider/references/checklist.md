# Provider Checklist

- Shared chat or embedding interface remains provider-neutral
- Provider SDK types stay inside the adapter
- Provider and model metadata are returned
- Credentials, endpoint, model, timeout, and retries are configurable
- Timeouts are finite and retries bounded
- Errors map to shared categories
- Conformance tests run without live network calls
- Sanitized fixtures contain no private prompts or health data
- Retrieval and coaching consume local evidence
- Numeric training facts remain deterministic
