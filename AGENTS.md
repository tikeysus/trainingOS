# TrainingOS Agent Guide

## Product

TrainingOS is a local-first personal running intelligence platform and long-term
data warehouse. Its primary use case is marathon training toward a 3:10 goal
(current PR 3:23; target race: Hamilton Marathon).

The product must help answer:

- Am I on track for 3:10, and with what confidence?
- How does this block compare with previous blocks?
- What explains changes in performance, heart rate, or fatigue?
- Is training load progressing appropriately?
- How are aerobic efficiency and recovery changing?

## Non-Negotiable Architecture

Data flow:

`sources -> nightly sync -> SQLite -> derived metrics -> retrieval -> UI/coach`

- Keep all durable data local. Dashboard and coach queries must never require
  live Garmin, Strava, weather, or LLM-provider calls for stored facts.
- Treat source services as replaceable adapters, not domain dependencies.
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
- Store the evidence used for summaries and projections. Never present an
  opaque score without its inputs, formula/version, and applicable caveats.
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

TrainingOS may store years of activities, notes, weather, health data, and
derived metrics in SQLite. Stored local data is not the limiting factor; the
limiting factor is the amount of retrieved evidence used in one coach answer.

Default coach answers should target about 20k tokens of active evidence. As a
rough guide, that is approximately:

- 100-200 short preference or context notes.
- 50-100 summarized activities.
- 20-40 detailed activity or week summaries.

Do not send raw Garmin, Strava, or FIT history wholesale to an LLM. Retrieve and
synthesize compact local evidence documents such as activity summaries, weekly
summaries, block summaries, note summaries, metric evidence, caveats, and data
gaps.

For broad questions such as race readiness, the coach should search the full
local history but include only the most relevant evidence, such as recent weekly
summaries, the current block, comparable prior blocks, key workouts, races,
recovery trends, and relevant notes.

The user should never see a generic context-window failure. When evidence is
constrained, the coach must say what was considered, what was included, what was
omitted, and how that affects confidence. Examples:

- Evidence overflow: "I found 430 matching activities and used the 80 most
  relevant."
- Ambiguous request: "This spans multiple training blocks; narrowing to the
  current block or a prior PR buildup would improve precision."
- Data insufficiency: "Confidence is low because HRV and sleep are missing for
  11 of the last 14 days."

Coach responses should disclose evidence scope in tangible terms when useful,
for example: "I used 42 of 780 activities, 12 weekly summaries, and 8 notes most
relevant to this question."

## Initial Delivery Priorities

1. SQLite schema, migrations, configuration, and raw-data retention.
2. Garmin/manual FIT ingestion with idempotent sync.
3. Activity and daily-health normalization plus weather enrichment.
4. Tested, versioned derived metrics and weekly summaries.
5. Dashboard backed only by local data.
6. Provider-agnostic retrieval and AI coach.

## Engineering Expectations

- Follow existing project conventions once established; keep changes scoped.
- Add tests for parsers, sync idempotency, migrations, metric formulas, and
  provider contracts. Use realistic fixtures with personal data removed.
- Document assumptions for physiological formulas and race predictions.
- Do not add infrastructure, abstractions, or cloud dependencies without a
  demonstrated need.
