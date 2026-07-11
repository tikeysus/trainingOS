# Ingestion Checklist

- Adapter is replaceable and vendor types do not escape it
- Raw artifact retention and lookup are defined
- Source, external ID, sync time, timezone, units, and provenance are stored
- Sync checkpoint is incremental, resumable, and auditable
- Database constraints back application-level deduplication
- Overlap and retry tests prove idempotency
- Partial failures do not advance checkpoints incorrectly
- Credentials and private payloads are absent from logs and fixtures
- FIT fixtures are explicitly sanitized
- Dashboard and coach remain local-data-only
