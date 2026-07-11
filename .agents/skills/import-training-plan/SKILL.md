---
name: import-training-plan
description: Import or revise running training plans from Markdown or similar documents into TrainingOS. Use when structuring planned workouts, validating plan dates and units, preserving plan revisions, previewing changes, or implementing idempotent training-plan imports. Do not use for completed activity ingestion.
---

# Import Training Plan

## Workflow

1. Inspect existing plan domain models, migrations, import conventions, and tests.
2. Parse the source into explicit plan, week, day, and session records. Preserve the original document and source identity when raw retention exists.
3. Normalize dates, timezones, distances, durations, paces, and intensity labels without silently guessing. Represent optional sessions explicitly.
4. Preserve revisions with a stable plan identity plus source revision or content hash. Do not overwrite historical plan text without an audit trail.
5. Validate chronology, duplicates, units, race date alignment, and ambiguous or missing required fields.
6. Produce a dry-run preview showing creates, updates, unchanged records, warnings, and rejected rows before persistence.
7. Make repeated imports idempotent and transactional. A retry must not duplicate plans, weeks, or sessions.
8. Add parser, validation, revision, preview, and repeated-import tests with sanitized fixtures.

Read [references/checklist.md](references/checklist.md) before finalizing the change.

## Boundaries

- Keep planned workouts separate from completed activities.
- Record provenance for every imported plan and revision.
- Report assumptions; never invent dates, units, or workout intent.
- Keep import logic deterministic and independent of AI providers or external services.
