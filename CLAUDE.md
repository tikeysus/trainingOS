# TrainingOS: Local-First Running Intelligence Platform

TrainingOS is a local-first running intelligence platform: Garmin sync → SQLite normalization → deterministic metric derivation → evidence-grounded AI coaching.

## Core Principles

**Non-Negotiable Architecture:**
- All durable data stays local; treat external sources (Garmin, weather) as replaceable adapters.
- Preserve raw payloads; normalize into canonical SQLite schema.
- Sync is incremental, idempotent, resumable, auditable via `SyncRunner` protocol.
- Compute expensive derived metrics during sync, not on dashboard requests.
- Version all metric formulas (`MethodVersion`) for deterministic reproducibility.
- Define stable internal domain models before shaping code around vendor APIs.
- Query Claude with retrieved local evidence only; never send raw activity history wholesale.

**Implementation Boundaries:**
- Keep ingestion, normalization, analytics, retrieval, provider adapters, and presentation separated.
- Use Claude API exclusively via Anthropic for all LLM tasks; no alternative providers. 
- Store evidence behind every summary and projection. 
- Use explicit units and timezone-aware timestamps; avoid silent conversions.
- Protect credentials and personal health data; never commit secrets.
- Prefer deterministic code for calculations; use LLMs for explanation and synthesis only.

## Data Domains

- **Activities:** Summary data, laps/splits, high-resolution FIT samples (altitude, cadence, distance, heart_rate, power, speed, temperature).
- **Daily health:** Sleep, HRV, resting HR, stress, Body Battery, SpO2, VO2 max.
- **Context:** User notes (illness, injury, travel, stress), races, training blocks, weather at workout time.
- **Analytics:** Zones (from HR %), mileage/load trends, long runs, marathon-pace volume, aerobic efficiency, HR drift, fatigue/recovery metrics, race projections.
- **Retrieval documents:** Compact evidence summaries for coach LLM: activities, workouts, weeks, notes, races, training blocks (generated from analytics + raw records).

Garmin is the preferred rich-data source. Manual FIT/JSON export-import remains a viable fallback. Strava integration deferred.

## Key Architectural Patterns

### Provider Abstraction
- Define provider contracts as Protocols (`ChatProvider`, `EmbeddingProvider`; see `providers/__init__.py` lines 126-133).
- Implement via `AnthropicChatProvider`; use `FakeChatProvider` for deterministic tests.
- Provider layer owns error classification (`ProviderErrorCategory`: authentication, rate limit, timeout, 5xx; lines 344-360).
- Retry logic is provider-scoped, not caller-facing; retry policy uses linear backoff (1s, 2s, 3s...).
- Health checks are provider responsibility; coach service queries provider health on demand (line 391).

### Sync & Ingestion Orchestration
- Sync is single-threaded, sequential, auditable: `SyncRunner` (see `ingestion/sync.py` lines 1-365) coordinates one source adapter at a time.
- Adapters are Protocols (`SyncAdapter[PayloadT]`); cursor format is source-specific (Garmin: `ISO8601|activity_id`; FIT: line index). Runner never interprets cursors.
- Transaction per record: `BEGIN IMMEDIATE` before handler invocation, commit/rollback after (line 252).
- Cursor advancement and disposition (`IMPORTED`/`SKIPPED`) are atomic within same transaction (line 320).
- Failed records leave checkpoint at last successful record; resumable on retry.
- Retry classification: only `SyncError.retryable=True` errors trigger retries; unexpected exceptions fail fast (line 283).
- Raw payloads deduplicated by SHA256 before sync; manifests stored in `raw_source_records` table with source, external_id, checksum (line 340).

### Normalization & Unit Handling
- `NormalizationStore` (see `normalization/store.py` lines 35-410) upserts canonical domain records without committing; caller controls transaction scope.
- Unit conversion is explicit with validation (lines 35-60): catches silent m/km, s/ms, %/ratio errors.
- Provenance replacement rules (line 370): `USER_ENTERED` and `IMPORTED` records can be replaced; `COMPUTED` records append as new evidence for audit trail.
- All timestamps UTC internally; convert to local timezone only for display/user input.

### Analytics & Metric Versioning
- Derived metrics computed during sync, not on dashboard requests (see `analytics/metrics.py` lines 14-650).
- Weekly summaries: distance, duration, run count, average pace, aerobic efficiency, HR drift, recovery coverage, training load trend, marathon-pace volume.
- Block-level totals: phase-level mileage, peak weeks, comparisons to prior blocks (see `analytics/blocks.py`).
- All metrics tied to `MethodVersion` (e.g., `WEEKLY_SUMMARY_METHOD = MethodVersion("weekly_summary", "1.0.0")`, line 14). Historical results reproducible.
- Evidence tables link metrics to source records via `metric_evidence`, `provenance_evidence`, `provenance_caveats` (line 650+).

### Retrieval & Evidence Generation
- Retrieval documents (see `retrieval/__init__.py` lines 27-930) are compact summaries: activity, workout, week, note, race, training_block types.
- Document generation creates human-readable summaries with evidence record IDs, formula caveats, data gaps embedded.
- FTS5 full-text search (BM25 ranking, line 119); stale documents excluded; stale reasons tracked for audit.
- Token budgeting (line 168): coach service can truncate documents to stay under ~20k token budget (~4 chars per token estimate). Omitted docs tracked for transparency.

### Presentation & Visualization
- Coach UI (`coach_web.py`) is the single unified dashboard interface; no external dashboarding tools (Grafana removed).
- Visualizations are markdown-native and generated on-demand: time-series charts, trend sparklines, pace distributions as interactive HTML (via `plotly`/`altair`/`vega-lite`).
- Coach retrieval layer generates chart specs alongside evidence summaries; coach UI renders them inline in markdown.
- Chart generation is deterministic (no external calls); charts are ephemeral (not persisted, regenerated on query).

## Implementation Status

**Delivered:**
1. ✓ SQLite schema, migrations (12 migrations, 794 lines in `storage/sql/`), raw-data retention
2. ✓ Garmin ingestion with idempotent sync (`SyncRunner` with cursor validation)
3. ✓ Activity + daily-health normalization, weather enrichment
4. ✓ Derived metrics (weekly, block-level) with versioned formulas
5. ✓ Dashboard APIs backed by local data only (`coach_web.py` HTTP server)
6. ✓ Claude-powered retrieval and AI coach with per-query model selection

**Known Gaps (deferred):**
- Intensity scoring (proposed formula in codebase; not yet normalized to Activity model)
- HR zone derivation from samples (raw HR samples exist; zone computation absent)
- Race projections (code skeleton exists; not yet integrated into coach workflow)

## Engineering Practices

**Testing:**
- Write exhaustive test cases BEFORE implementation.
- Use realistic fixtures (Garmin JSON, FIT samples, daily health snapshots) with personal data removed.
- Providers: use `FakeChatProvider` (deterministic request history, no external calls).
- Database: in-memory SQLite with full schema per test via `conftest.py` fixtures.
- Fixture scoping: function-level isolation; builder patterns for domain objects (e.g., `sample_activity()`, `sample_daily_health()`).
- Current test coverage: 30 active test files, ~538 test methods (sync idempotency, FIT/JSON parsing, analytics formulas, coach service, migrations well-covered).
- Test gaps: direct unit tests for `notes.py` internals and `presentation/__init__.py` helpers (question classification, prompt generation, caveat formatting); see `tests/pytest_pending/` for draft bug reproduction tests.

**Database:**
- 12 ordered migrations in `storage/sql/NNN_*.sql`; applied transactionally via `apply_migrations()`.
- Schema immutable once committed; rollback not supported (migration chain integrity).
- SQLite WAL mode enabled for concurrent reads during sync.
- Tests: fresh in-memory SQLite per test with full schema.

## Known Issues

- `tests/pytest_pending/` holds draft bug-reproduction tests (`test_bugs_before_and_after.py`, `test_critical_bugs.py`); intentionally deferred and not yet triaged. Leave as-is until explicitly picked up.
- **Embedding provider naming error:** `.agents/parallel-issues-plan.md`, `.agents/worktree-guides.md`, and `.agents/WORKTREE_REBASE_GUIDE.md` currently direct issue #49 to implement `AnthropicEmbeddingProvider` targeting `text-embedding-3-small`. That model name is OpenAI's, not Anthropic's — Anthropic does not publish an embeddings API. Before implementing #49, pick a real embedding source (e.g. a local/open-weight model, or an actual third-party embeddings API) and correct those `.agents/` docs; do not build against `text-embedding-3-small` as specified.
