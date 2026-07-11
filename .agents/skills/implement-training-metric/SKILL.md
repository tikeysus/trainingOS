---
name: implement-training-metric
description: Implement or revise deterministic running analytics and derived metrics in TrainingOS. Use for training load, aerobic efficiency, heart-rate drift, recovery, fatigue, race projections, trends, or weekly summaries that need formulas, units, versions, evidence, uncertainty, and tests.
---

# Implement Training Metric

## Workflow

1. Locate the metric computation layer, domain inputs, persistence model, formula version convention, and reporting consumers.
2. Write the formula and assumptions before coding. Define inputs, exclusions, units, timezone behavior, missing-data behavior, valid ranges, and physiological limitations.
3. Implement deterministic calculation code. Do not use an LLM to produce numeric metric values.
4. Assign a stable formula version. Persist the version, evidence references, value, units, computation time, and caveats.
5. Represent uncertainty explicitly for estimates and projections. Avoid unsupported causal claims and false precision.
6. Compute expensive values during sync or batch derivation, not dashboard requests.
7. Add hand-calculated fixtures, boundaries, missing-data cases, unit tests, and regression tests across formula versions.
8. Expose observed facts, computed estimates, and interpretation as distinct downstream concepts.

Read [references/checklist.md](references/checklist.md) before finalizing the change.

## Boundaries

- Prefer transparent formulas over opaque scores.
- Never silently convert units or use naive timestamps.
- Keep historical results reproducible after formula changes.
- Treat health implications as informational, not diagnostic.
