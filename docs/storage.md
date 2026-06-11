# Local storage

TrainingOS uses SQLite for durable local data. The database path defaults to
`~/.local/share/trainingos/trainingos.sqlite3` and can be overridden with:

```sh
export TRAININGOS_DB_PATH=/path/to/trainingos.sqlite3
```

Database files, local environment files, credentials, and personal source data
must not be committed.

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
  either inline payload bytes or a local storage path.
- Source references and sync audit rows support idempotent import and resume
  behavior without embedding vendor types in canonical tables.
- Per-source checkpoints advance transactionally with successful handler
  writes. Retry attempts and safe error details remain queryable by sync run.
- Computed records persist method name, method version, evidence links, and
  caveats so historical values remain explainable.
