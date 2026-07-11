# Phase 1 Worktree Implementation Guides

## Quick Start

All Phase 1 worktrees are ready. To start working on an issue:

```bash
cd /Users/tikeysus/Documents/projects/trainingOS/worktrees/<WORKTREE>/
git branch -vv  # Verify branch
git log -1      # See latest commit
```

Each worktree is isolated and can be worked on independently. After implementing and testing, create a PR from your branch back to master.

---

## #61: [Database] Add intensity and Garmin self-evaluation columns

**Path:** `worktrees/61-database-intensity/`

**Deliverable:** Database migration `013_add_activity_intensity_fields.sql`

### Implementation Checklist
- [ ] Create `src/trainingos/storage/sql/013_add_activity_intensity_fields.sql`
- [ ] Add `perceived_effort` column (INTEGER, CHECK 0-10, nullable)
- [ ] Add `perceived_mood` column (INTEGER, CHECK 1-5, nullable)
- [ ] Add `intensity_score` column (REAL, CHECK 0-10, nullable)
- [ ] Write migration up/down (idempotent)
- [ ] Test on existing DB with 234 activities
- [ ] Update `conftest.py` to apply migration in test fixtures

### Key Files
- `src/trainingos/storage/sql/` — migrations directory
- `tests/conftest.py` — applies all migrations to test DB
- `src/trainingos/storage/store.py` — uses schema

### Test Requirements
```python
def test_migration_adds_intensity_columns():
    # Create DB with 012, apply 013, verify schema
    # Verify CHECK constraints work
    # Test NULL defaults
```

### Notes
- Blocks: #62, #64, #65
- Once merged, notify team that Phase 2 can begin

---

## #63: [FIT Parser] Extract Garmin self-evaluation (effort and mood)

**Path:** `worktrees/63-fit-parser-effort/`

**Deliverable:** Update `src/trainingos/ingestion/fit.py` to extract effort/mood

### Implementation Checklist
- [ ] Read Garmin FIT file format docs (links in issue)
- [ ] Locate field extractors in `src/trainingos/ingestion/fit.py` (around line 192-205)
- [ ] Extract `perceived_effort` field (Garmin 0-10 scale)
- [ ] Extract `perceived_mood` field (Garmin 1-5 scale)
- [ ] Return as part of FIT sample dict
- [ ] Handle missing fields gracefully (None → skip)
- [ ] Add unit tests with real Garmin FIT samples

### Key Files
- `src/trainingos/ingestion/fit.py` — FIT parser
- `tests/fixtures/garmin_*.fit` — test FIT files
- `src/trainingos/ingestion/sync.py` — uses parsed data

### Test Requirements
```python
def test_fit_parser_extracts_effort_and_mood():
    # Load real Garmin FIT file
    # Parse with extracted fields
    # Assert effort in [0, 10] and mood in [1, 5]
```

### Notes
- Independent of #61 (develop in parallel)
- Feeds into: #62, #64
- FIT format reference: search "Garmin FIT profile" or use `fitparse` library

---

## #69: [Training Blocks] Define Phase 2 and Phase 3 marathon training plans

**Path:** `worktrees/69-training-blocks-phases/`

**Deliverable:** Domain data for marathon training phases

### Implementation Checklist
- [ ] Review existing training_blocks schema (e.g., Phase 1 definition if exists)
- [ ] Define Phase 2 structure: weeks, focus (build, aerobic, strength), key workouts
- [ ] Define Phase 3 structure: weeks, focus (taper, peak), key workouts
- [ ] Add to training_blocks table or configuration
- [ ] Document phases in `docs/` or CLAUDE.md
- [ ] Create seed data or fixtures for testing
- [ ] Add tests to verify phase definitions load correctly

### Key Files
- `src/trainingos/storage/store.py` — training_blocks table definition
- `docs/coach.md` or similar — document phases
- `src/trainingos/models.py` — TrainingBlock dataclass

### Test Requirements
```python
def test_training_block_phases():
    # Load Phase 2 and Phase 3 definitions
    # Verify structure, dates, focus areas
```

### Notes
- Independent feature
- Can be implemented as code constants, DB seed, or configuration
- No schema changes needed unless extending training_blocks

---

## #43: [Weather Enrichment] Implement Open-Meteo integration

**Path:** `worktrees/43-weather-enrichment/`

**Deliverable:** Weather provider interface + Open-Meteo implementation

### Implementation Checklist
- [ ] Create `src/trainingos/weather/provider.py` with `WeatherProvider` protocol
- [ ] Implement `OpenMeteoWeatherProvider` (HTTP client for historical weather)
- [ ] Add weather enrichment step to normalization pipeline
- [ ] Persist weather rows to `weather_observations` table
- [ ] Make enrichment idempotent (skip if observation already exists)
- [ ] Add to `python3 -m trainingos.refresh` workflow with optional skip flag
- [ ] Write comprehensive tests (mock HTTP responses)

### Key Files
- `src/trainingos/weather/` — new module
- `src/trainingos/normalization/store.py` — persist weather
- `src/trainingos/storage/store.py` — weather_observations table
- `src/trainingos/refresh.py` or similar — orchestration

### Test Requirements
```python
def test_weather_enrichment_open_meteo():
    # Mock HTTP response
    # Call enricher with activity (has GPS coords)
    # Verify weather row created with temp, wind, precip

def test_weather_enrichment_idempotent():
    # Run enrichment twice
    # Verify no duplicate rows

def test_weather_enrichment_no_gps():
    # Activity with no coordinates
    # Verify skipped gracefully
```

### Notes
- Independent feature
- Offline flag must disable HTTP calls
- Open-Meteo: free, no API key, supports historical queries
- Reference: https://open-meteo.com/en/docs/historical-weather-api

---

## #47: [Dashboard] Daily health panels (sleep, HRV, resting HR, Body Battery)

**Path:** `worktrees/47-health-dashboard/`

**Deliverable:** Coach UI endpoints and views for daily health metrics

### Implementation Checklist
- [ ] Create `dashboard_daily_health` reporting view
- [ ] Expose: date, resting_hr, hrv_rmssd, sleep_duration_s, sleep_score, body_battery_eod, stress_avg, spo2_avg
- [ ] Handle NULL gracefully (no zero-fill, no errors)
- [ ] Add coach UI endpoints: daily health summary, trends, recovery coverage
- [ ] Overlay context notes (illness, injury) as annotations
- [ ] Support 90-day history on default range
- [ ] Test with synthetic daily_health fixtures

### Key Files
- `src/trainingos/coach_web.py` — HTTP server for coach UI
- `src/trainingos/presentation/` — if structured this way
- `src/trainingos/models.py` — DailyHealth dataclass

### Test Requirements
```python
def test_dashboard_daily_health_view():
    # Insert 30 days of daily_health
    # Query view
    # Verify rows, NULL handling, date range

def test_dashboard_health_with_context_notes():
    # Add context note (injury) overlapping dates
    # Verify annotation appears
```

### Notes
- Depends on existing `daily_health` table (populated by issue #20)
- Can develop with fixtures if #20 not yet merged
- No schema changes needed

---

## #49: [Retrieval] Embedding-based search with FTS hybrid re-rank

**Path:** `worktrees/49-embedding-retrieval/`

**Deliverable:** Embedding provider interface + hybrid retrieval

### Implementation Checklist
- [ ] Create `src/trainingos/providers/embedding.py` with `EmbeddingProvider` protocol
- [ ] Implement `OllamaEmbeddingProvider` (or similar)
- [ ] Create `retrieval_embeddings` migration table (document_id, model_name, model_version, vector_blob)
- [ ] Add hybrid retrieval logic: FTS candidate → embed query → cosine score → re-rank
- [ ] Integrate into `CoachService` (fallback to FTS-only if unavailable)
- [ ] Add `python3 -m trainingos.refresh` embedding generation step
- [ ] Update `/api/health` to report embedding status
- [ ] Write tests with mocked embeddings

### Key Files
- `src/trainingos/providers/embedding.py` — new protocol + implementation
- `src/trainingos/retrieval/__init__.py` — hybrid retrieval logic
- `src/trainingos/coach_service.py` — integration point
- `src/trainingos/storage/sql/` — new migration for embeddings table

### Test Requirements
```python
def test_embedding_provider_generates_vectors():
    # Mock provider
    # Embed sample text
    # Verify float32 vector

def test_hybrid_retrieval_re_ranks():
    # FTS candidates + query embedding
    # Verify cosine similarity scores
    # Verify re-ranked order matches expectation

def test_retrieval_fallback_to_fts():
    # Disable embedding provider
    # Verify fallback to FTS-only works
```

### Notes
- Independent feature (can use mocked embeddings)
- Ollama's `nomic-embed-text` is 768-dimensional
- FTS-only fallback critical for offline mode
- Environment variables: `TRAININGOS_AI_EMBEDDING_PROVIDER`, `TRAININGOS_OLLAMA_EMBEDDING_MODEL`

---

## Workflow for Each Worktree

### 1. Start Work
```bash
cd worktrees/<branch>/
git status
git log -1
```

### 2. Write Tests First (per CLAUDE.md)
```bash
# In tests/, create test_<feature>.py
# Write exhaustive test cases
# Commit: git commit -m "test(<feature>): add comprehensive test cases"
```

### 3. Implement Feature
```bash
# Implement feature in src/
# Run tests: pytest tests/test_<feature>.py -v
# Commit logical chunks with conventional commits
```

### 4. Final Checks
```bash
pytest tests/ -v  # All tests pass
pylint src/       # Code quality
git diff master   # Review all changes
```

### 5. Create PR
```bash
git push -u origin <branch-name>
# Then open PR on GitHub: gh pr create --title "..." --body "..."
```

---

## Dependency Notifications

When your issue is complete and merged:

| Issue | When Merged | Notify |
|-------|------------|--------|
| #61 | Database ready | Start #62 |
| #63 | FIT parsing ready | Use in #62 |
| #69 | Training phases ready | Use in #66 API |
| #43 | Weather enrichment ready | Dashboard/coach can use |
| #47 | Health dashboard ready | Feature complete |
| #49 | Embedding retrieval ready | Coach can use hybrid search |

After Phase 1, Phase 2 issues (#62, #64) become unblocked and can begin.

