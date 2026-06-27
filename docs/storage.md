# Local storage

TrainingOS uses SQLite for durable local data. The database path defaults to
`~/.local/share/trainingos/trainingos.sqlite3` and can be overridden with:

```sh
export TRAININGOS_DB_PATH=/path/to/trainingos.sqlite3
```

## Repository hygiene

The following must never be committed:

- SQLite database files (`*.sqlite3`, `*.sqlite`, `*.db`, WAL/SHM sidecars).
- Raw Garmin export zips and real FIT activity files. These contain precise
  GPS tracks, heart-rate data, and other personal health information.
- Environment files (`.env`, `.env.*`) and anything under `data/`, `imports/`,
  `private/`, `raw/`, or `secrets/`.
- macOS metadata (`.DS_Store`) and Python build artefacts (`__pycache__/`,
  `*.pyc`, `.venv/`).

All of the above are covered by `.gitignore`. `tests/test_gitignore.py`
verifies each category so regressions are caught by the test suite.

### Test fixtures

Sanitized FIT files stripped of personal data may be committed under
`tests/fixtures/sanitized/`. That path is explicitly allowed in `.gitignore`
via the `!tests/fixtures/sanitized/*.fit` exception. All other `*.fit` files
remain blocked. Before adding a fixture, confirm it contains no real GPS
coordinates, timestamps, or health measurements that could identify a person.

Raw FIT/API artifacts default to `~/.local/share/trainingos/raw` and can be
overridden with:

```sh
export TRAININGOS_RAW_DATA_DIR=/path/to/raw-artifacts
```

## Migrations

Ordered SQL migrations live in `trainingos.storage.sql` and use filenames such
as `001_initial_schema.sql`. `apply_migrations` applies each pending migration
in its own transaction and records its SHA-256 checksum in
`schema_migrations`.

Applied migrations are immutable. Editing or removing one causes startup to
fail rather than silently changing historical schema behavior. Schema changes
must be added as a new, consecutively numbered migration.

SQLite does not provide a general automatic rollback for arbitrary schema
changes. A failed migration transaction is rolled back immediately. If a
migration was already applied and later proves incorrect, restore the local
database from backup or add a forward repair migration; do not rewrite the
applied migration.

## Schema conventions

- UTC timestamps are stored as ISO 8601 text.
- Calendar dates are stored as ISO 8601 text and interpreted using the IANA
  timezone on their parent `records` row.
- Canonical quantities use explicit unit columns or unit-specific column names.
- Raw source records retain source identity, checksum, ingestion time, and
  either inline payload bytes or a local storage path. FIT files are retained
  as content-addressed local files and referenced from SQLite.
- Source references and sync audit rows support idempotent import and resume
  behavior without embedding vendor types in canonical tables.
- Each normalized source identity maps to one local record and references a
  retained raw artifact whose sync run remains auditable. One raw artifact can
  be evidence for multiple normalized entities, such as FIT samples and laps.
- Per-source checkpoints advance transactionally with successful handler
  writes. Retry attempts and safe error details remain queryable by sync run.
- Computed records persist method name, method version, evidence links, and
  caveats so historical values remain explainable.

## Normalized writes

`NormalizationStore` writes activities, laps, activity samples, daily health,
context notes, and weather observations through vendor-neutral domain models.
It does not commit, so a sync handler can persist normalized rows and advance
its checkpoint in one transaction.

Normalized imported records require at least one source reference backed by a
retained raw source record. The raw record links the canonical entity to its
sync run. Source references also retain the parser name and version. Local
user-entered records, such as context notes and manual weather, use
`user_entered` provenance and do not require source references.

Context notes are timestamped, typed, editable, and can link to existing local
records such as activities, races, or training blocks. Weather observations are
linked to activities through SQLite and store observation time, timezone,
source, raw lineage, explicit units, and metric availability. Weather
enrichment runs before query time; dashboards and coach retrieval read these
persisted rows and do not call live weather services.

Upsert precedence is deterministic:

- An existing core record is replaced only by a record with an equal or newer
  `updated_at`; stale records can refresh source audit metadata but not facts.
- A measured metric is never replaced by unavailable or unsupported status.
- Among measured values, an incoming value replaces the existing value only
  when its quality is equal or higher. Missing quality is treated as `0.0`.
- Unsupported or unavailable values may replace one another on a newer record.

Metric rows store measured, unavailable, and unsupported states separately.
Measured zero is therefore distinct from no source value and from a metric the
source cannot provide.
