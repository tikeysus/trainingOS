# TrainingOS Agent Guide

## Garmin Self-Evaluation & Intensity

**Garmin feature:** [Self-Evaluation](https://support.garmin.com/en-US/?faq=8nISJXqSZVAI3Td4IWRqsA) prompts after activities to capture two separate user inputs, stored in FIT files:
- **Perceived Effort** (0-10 scale): Physiological difficulty — "how hard did I push?"
- **Perceived Mood** (5-point scale: Very Weak → Weak → OK → Strong → Very Strong): Recovery/subjective state — "how strong did I feel?"

Both are stored in FIT session records; extraction code exists (fitdecode) but fields not yet captured in Activity model.

**Garmin data state:**
- 8 activity .fit exports in `var/garmin_activities/` (2025-02-04 to 2026-06-16)
- 234 activities normalized into SQLite but missing perceived_effort and perceived_mood columns
- Current schema: activities(record_id, activity_type, started_at, duration_seconds, distance_metres, title)
- Metrics stored per-sample: altitude, cadence, distance, heart_rate, power, speed, temperature
- HR zones: not yet computed from samples; will derive at sync time (5 zones from % of max HR)

**Intensity scoring (proposed formula):**
Uses weighted average of pace, HR zone, Garmin effort, weather, perceived mood to produce 0-10 intensity score. Formula components:
- Pace (25%): fast efforts score high, recovery runs low
- HR zone (25%): Z5 high, Z1-Z2 low
- Perceived effort (30%): direct user input (0-10 normalized)
- Weather (10%): temperature/humidity (hot/humid increases effort perception)
- Perceived mood (10%): inverse of mood scale (Very Strong = low intensity felt, Very Weak = high intensity felt)

Stored as `intensity_score REAL CHECK (intensity_score BETWEEN 0.0 AND 10.0)` on activities table.

## Pending: Lock Down Personal Data
Once dashboard is deployed, add `var/garmin_activities/` and `var/76e88aeb-*/` to `.claudeignore` to prevent raw GDPR export files from entering context.

## Product

TrainingOS: local-first running intelligence platform. 

## Non-Negotiable Architecture

Data flow:

`sources -> nightly sync -> SQLite -> derived metrics -> retrieval -> UI/coach`

- Keep all durable data local; never require live Garmin, Strava, weather, or
  LLM calls for stored facts. Treat source services as replaceable adapters.
- Preserve raw source payloads/FIT files when practical; normalize into SQLite.
- Make sync jobs incremental, idempotent, resumable, and auditable.
- Record source, external ID, sync time, timezone, units, and metric provenance.
- Compute expensive derived metrics during sync, not dashboard requests.
- Version derived-metric formulas so historical results can be reproduced.
- Prefer simple SQLite-backed designs until scale proves they are insufficient.

## Data Domains

- Activities: summary data, laps/splits, and high-resolution FIT samples.
- Daily health: sleep, HRV, resting HR, stress, Body Battery, SpO2, VO2 max.
- Context: user notes, illness/injury/travel/stress, weather at workout time.
- Analytics: zones, mileage/load trends, long runs, marathon-pace volume,
  aerobic efficiency, HR drift, fatigue/recovery, and race projections.
- Retrieval documents: activity, workout, week, note, race, and block summaries.

Garmin is the preferred rich-data source; Strava and future sources are
optional adapters. Manual exports/imports must remain viable fallbacks.

## Implementation Boundaries

- Keep ingestion, normalization, analytics, retrieval, provider adapters, and
  presentation separated.
- Define stable internal domain models before shaping code around vendor APIs.
- Isolate LLM and embedding providers behind interfaces configurable for
  OpenAI, Anthropic, Gemini, Ollama, or local alternatives.
- Store the evidence behind every summary and projection; always expose inputs,
  formula version, and caveats — never an opaque score.
- Use explicit units and timezone-aware timestamps. Avoid silent conversions.
- Protect credentials and personal health/location data; never commit secrets.
- Prefer deterministic code for calculations. Use LLMs for explanation and
  synthesis, not as the source of numeric training metrics.

## Coaching Behavior

- Ground answers in retrieved local evidence: activities, recovery, weather,
  notes, races, and prior blocks.
- Distinguish observed facts, computed estimates, and model interpretation.
- Include relevant dates, trends, uncertainty, and data gaps.
- Avoid false precision and unsupported causal claims.
- Treat injury and health guidance as informational, not medical diagnosis.

## Context and Evidence Budget

Default coach answers target ~20k tokens of active evidence:

- 100-200 short preference or context notes.
- 50-100 summarized activities.
- 20-40 detailed activity or week summaries.

Retrieve and synthesize compact local evidence (activity/weekly/block summaries,
notes, metric evidence, caveats, data gaps) — never send raw source history
wholesale to an LLM.

For broad questions such as race readiness, the coach should search the full
local history but include only the most relevant evidence, such as recent weekly
summaries, the current block, comparable prior blocks, key workouts, races,
recovery trends, and relevant notes.

When evidence is constrained, the coach must state what was considered,
included, omitted, and how that affects confidence. Examples:

- Evidence overflow: "I found 430 matching activities and used the 80 most
  relevant."
- Ambiguous request: "This spans multiple training blocks; narrowing to the
  current block or a prior PR buildup would improve precision."
- Data insufficiency: "Confidence is low because HRV and sleep are missing for
  11 of the last 14 days."

## Initial Delivery Priorities

1. SQLite schema, migrations, configuration, and raw-data retention.
2. Garmin/manual FIT ingestion with idempotent sync.
3. Activity and daily-health normalization plus weather enrichment.
4. Tested, versioned derived metrics and weekly summaries.
5. Dashboard backed only by local data.
6. Provider-agnostic retrieval and AI coach.

## Engineering Expectations

- Add tests for parsers, sync idempotency, migrations, metric formulas, and
  provider contracts. Use realistic fixtures with personal data removed.
- Document assumptions for physiological formulas and race predictions.
- Do not add infrastructure, abstractions, or cloud dependencies without a
  demonstrated need.
- Do not use Linear for issue tracking, planning, or task management; redirect
  any workflow that would create a Linear issue to GitHub Issues or a local
  document.
