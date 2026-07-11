---
name: ingest-fitness-data
description: Add or change Garmin, manual FIT, or other fitness-data ingestion in TrainingOS. Use for source adapters, raw payload retention, incremental sync, FIT parsing, normalization, provenance, deduplication, retries, or credential handling. Do not use for dashboard-only or derived-metric-only work.
---

# Ingest Fitness Data

## Workflow

1. Inspect the source adapter boundary, internal models, migrations, raw storage, sync audit tables, and fixtures.
2. Keep vendor payloads and types inside the adapter. Map them into stable internal activity, lap, sample, and daily-health models.
3. Retain raw payloads or FIT files when practical, referenced by immutable source and external identifiers. Never commit real payloads, tokens, or personal data.
4. Record source, external ID, sync time, source timestamp, timezone, units, parser version, and field-level provenance where values can conflict.
5. Use cursor or time-window checkpoints for incremental, resumable sync. Commit checkpoints only after normalized writes succeed.
6. Deduplicate with source-specific natural keys and database uniqueness constraints. Make retries and overlapping windows idempotent.
7. Isolate credentials in configuration or secret storage. Do not log tokens or raw private payloads.
8. Add sanitized fixtures and tests for parsing, normalization, duplicates, partial failure, retry, timezone, and unit conversion.

Read [references/checklist.md](references/checklist.md) before finalizing the change.

## Boundaries

- Do not call source services from dashboard or coach queries.
- Do not shape domain models around one vendor.
- Do not calculate expensive analytics in request paths.
- Keep manual export/import viable as a fallback.
