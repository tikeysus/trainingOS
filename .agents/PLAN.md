# TrainingOS Implementation Plan: 12 Issues Across 5 Sequential Phases

## Phases & Dependencies

```
Phase 1: Foundation & Independent (can parallelize, all ready now)
├─ #61 (DB: intensity columns)      1-2h    → blocks #62, #64, #65
├─ #63 (FIT: effort/mood parsing)   2-3h    → feeds #62, #64
├─ #69 (Training: phase definitions) 1-2h   → independent
├─ #43 (Weather: Open-Meteo)        3-4h    → independent
├─ #47 (Dashboard: daily health)    2-3h    → independent
└─ #49 (Retrieval: hybrid embed)    4-5h    → independent

Phase 2: Model & Analytics (after #61 merges)
├─ #62 (Activity model update)      1-2h    → depends on #61, feeds #64, #65
└─ #64 (Intensity score formula)    2-3h    → depends on #62, feeds #65

Phase 3: Sync Integration (after #64 merges)
└─ #65 (Sync: integrate intensity)  1-2h    → depends on #64, feeds #66

Phase 4: API Layer (after #65 merges)
└─ #66 (API: 7 new endpoints)       3-4h    → depends on #65, feeds #67

Phase 5: Frontend (after #66 merges)
├─ #67 (SvelteKit dashboard)        6-8h    → depends on #66
└─ #68 (Recharts charts)            (part of #67)
```

**Total ~37 hours; Phase 1 parallelizable to ~3 hours elapsed.**

---

## Phase 1 Issues (Parallel-Ready)

### #61: Database — Add intensity and mood columns

**Path:** `worktrees/61-database-intensity/`  
**Deliverable:** `src/trainingos/storage/sql/013_add_activity_intensity_fields.sql`

- **Key files:** `storage/sql/`, `tests/conftest.py`, `storage/store.py`
- **Test:** Verify migration adds `perceived_effort` (0-10, nullable), `perceived_mood` (1-5, nullable), `intensity_score` (0-10, nullable) columns with CHECK constraints; applies cleanly to existing DB with 234 activities.
- **Blocks:** #62, #64, #65

---

### #63: FIT Parser — Extract Garmin self-evaluation fields

**Path:** `worktrees/63-fit-parser-effort/`  
**Deliverable:** Update `src/trainingos/ingestion/fit.py` to extract `perceived_effort` and `perceived_mood` from FIT files.

- **Key files:** `ingestion/fit.py`, `tests/fixtures/garmin_*.fit`, `ingestion/sync.py`
- **Test:** Parse real Garmin FIT files, verify effort ∈ [0,10] and mood ∈ [1,5]; handle missing fields gracefully.
- **Skill reference:** `.agents/skills/ingest-fitness-data/SKILL.md` for sync/parser conventions.
- **Independent**

---

### #69: Training Blocks — Define Phase 2 & Phase 3 marathon plans

**Path:** `worktrees/69-training-blocks-phases/`  
**Deliverable:** Domain data (Phase 2 and Phase 3 structure: weeks, focus, key workouts) into `training_blocks` table or config.

- **Key files:** `models.py` or config, `storage/store.py`, `docs/` or `CLAUDE.md`
- **Test:** Load Phase 2 and Phase 3 definitions, verify structure/dates/focus areas.
- **Independent**

---

### #43: Weather Enrichment — Open-Meteo integration

**Path:** `worktrees/43-weather-enrichment/`  
**Deliverable:** New `src/trainingos/weather/` module with `WeatherProvider` protocol and `OpenMeteoWeatherProvider` implementation; integrate idempotent enrichment into normalization pipeline.

- **Key files:** `weather/provider.py` (new), `normalization/store.py`, `storage/store.py` (weather_observations table), `refresh.py` or orchestration layer
- **Test:** Mock HTTP responses; verify weather row created with temp/wind/precip; test idempotency (no duplicates on retry); handle activities with no GPS gracefully.
- **Reference:** https://open-meteo.com/en/docs/historical-weather-api
- **Independent**

---

### #47: Dashboard — Daily health panels

**Path:** `worktrees/47-health-dashboard/`  
**Deliverable:** Coach UI endpoints exposing daily health: resting_hr, hrv_rmssd, sleep_duration_s, sleep_score, body_battery_eod, stress_avg, spo2_avg (90-day history, handle NULLs gracefully, overlay context notes).

- **Key files:** `coach_web.py`, `presentation/` (if structured), `models.py` (DailyHealth)
- **Test:** 30 days of daily_health fixtures; query view; verify NULL handling, date range, context-note annotations.
- **Depends on:** `daily_health` table populated by issue #20.
- **Independent**

---

### #49: Retrieval — Embedding-based hybrid search

**Path:** `worktrees/49-embedding-retrieval/`  
**Deliverable:** `src/trainingos/providers/embedding.py` with `EmbeddingProvider` protocol and implementation; hybrid retrieval (FTS candidates → embed query → cosine re-rank); fallback to FTS-only if unavailable.

- **Key files:** `providers/embedding.py` (new), `retrieval/__init__.py`, `coach_service.py`, `storage/sql/` (embeddings table migration), `refresh.py`
- **Test:** Embedding provider generates float vectors; hybrid retrieval re-ranks correctly; fallback to FTS-only works when embeddings unavailable.
- **Skill reference:** `.agents/skills/implement-training-metric/SKILL.md` for versioning/evidence patterns.
- **⚠️ Note:** Before implementing, see `CLAUDE.md` § Known Issues — pick a real embedding source (Anthropic does not publish embeddings API; `text-embedding-3-small` is OpenAI's). Update provider/model before coding.
- **Independent**

---

## Phase 2 Issues (Sequential, after Phase 1)

### #62: Activity Model — Update for intensity and mood

**Path:** `worktrees/62-activity-model/`  
**Deliverable:** Update `Activity` dataclass to include `perceived_effort`, `perceived_mood`, `intensity_score` fields.

- **Key files:** `models.py`, `normalization/store.py`
- **Test:** Activity instances accept new fields; serialization/deserialization works.
- **Depends on:** #61 (schema exists)
- **Feeds:** #64, #65

---

### #64: Analytics — Intensity score formula

**Path:** `worktrees/64-intensity-analytics/`  
**Deliverable:** Deterministic intensity computation (proposed: pace 25% + HR zone 25% + effort 30% + weather 10% + mood 10%); versioned formula; evidence linkage; comprehensive unit tests.

- **Key files:** `analytics/metrics.py`, `models.py`
- **Test:** Hand-calculated fixtures, boundary cases, missing-data behavior, regression tests across formula versions.
- **Skill reference:** `.agents/skills/implement-training-metric/SKILL.md` for deterministic metric conventions.
- **Depends on:** #62 (Activity model has new fields)
- **Feeds:** #65

---

## Phase 3 Issue (Sequential, after Phase 2)

### #65: Sync Integration — Compute intensity during daily_sync

**Path:** `worktrees/65-sync-intensity/`  
**Deliverable:** Call `compute_intensity_score()` during activity normalization in sync pipeline.

- **Key files:** `ingestion/sync.py`, `analytics/metrics.py`
- **Test:** Intensity computed and stored for each synced activity; formula version persisted.
- **Depends on:** #64 (intensity computation ready)
- **Feeds:** #66

---

## Phase 4 Issue (Sequential, after Phase 3)

### #66: API Layer — New endpoints

**Path:** `worktrees/66-api-endpoints/`  
**Deliverable:** 7 new endpoints: `/api/activities`, `/api/activities/{id}`, `/api/weekly-summaries`, `/api/training-blocks`, `PATCH /api/activity/{id}`, plus helpers.

- **Key files:** `coach_web.py`
- **Test:** Each endpoint returns expected schema; filters/pagination work; PATCH updates correctly.
- **Depends on:** #65 (data normalized)
- **Feeds:** #67

---

## Phase 5 Issues (Sequential, after Phase 4)

### #67: Frontend — SvelteKit dashboard with charts

**Path:** `worktrees/67-sveltekit-dashboard/`  
**Deliverable:** Single-page SvelteKit app; MDX markdown structure; glassmorphism styling; time navigation; filters; Recharts components (distance, pace, HR, zones).

- **Key files:** `src/routes/`, `src/components/`, `tailwind.config.js`
- **Test:** Dashboard loads; time navigation works; charts render correctly.
- **Depends on:** #66 (all API endpoints stable)

---

### #68: Charts — Recharts implementation

Integrated into #67 (same worktree).

---

## Key Pointers

- **Testing:** Exhaustive test cases *before* implementation (per `CLAUDE.md`).
- **Database:** In-memory SQLite per test, full schema via `conftest.py` fixtures.
- **Fixtures:** Sanitized realistic Garmin/FIT/daily-health payloads; no personal data committed.
- **Metrics:** Deterministic formulas, versioned, with evidence linkage. See `.agents/skills/implement-training-metric/SKILL.md`.
- **Ingestion:** Idempotent sync, cursor-based checkpoints, raw payload retention. See `.agents/skills/ingest-fitness-data/SKILL.md`.
- **Provider:** Claude via Anthropic API exclusively; no alternative providers.
- **Git:** Conventional commits (feat:, fix:, chore:, test:); logical chunks; no `--no-verify`.

---

## When an Issue is Done

1. All acceptance criteria met
2. Tests pass (`pytest tests/ -v`)
3. Commit to branch; push; create PR
4. Notify dependent issues (e.g., when #61 merges, #62 can start)
