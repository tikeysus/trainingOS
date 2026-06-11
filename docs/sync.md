# Sync framework

TrainingOS runs source adapters through `trainingos.ingestion.SyncRunner`.
Adapters expose vendor-neutral record envelopes with an external ID, opaque
payload, and a high-water cursor representing the durable position after that
record. `cursor_after` must never precede the supplied checkpoint or the prior
record's cursor, even when an adapter returns an overlapping window. Equality
is allowed. Because cursor formats are source-specific, adapters implement
`cursor_is_at_or_after` so the runner can reject and audit regressions before
processing a record. Vendor SDK types may exist inside an adapter and its
normalization handler, but they must not enter domain models.

Each source has one durable checkpoint in `sync_checkpoints`. For a normal
run, the handler's SQLite writes, run counts, and checkpoint advancement commit
in one transaction per record. A failed record rolls back all three, so a later
run resumes from the last durable record. Handlers must also use source natural
keys and uniqueness constraints because adapters may intentionally return
overlapping windows.

## Retry behavior

`RetryPolicy` defaults to three attempts with linear waits of one, then two
seconds. Only `SyncError(..., retryable=True)` is retried. Retry attempts and
safe public messages are recorded in `sync_errors`; unexpected exception
details are not persisted because they may contain credentials or private
payload data. Exhausted retries fail the run and leave its checkpoint at the
last successful record.

## Dry runs

Dry runs execute each handler inside a transaction and roll it back. Counts and
the final in-memory cursor remain available in the run report, but normalized
writes and durable checkpoints do not change. Handlers used for dry runs must
keep their effects inside the supplied SQLite connection.

## Local scheduling

An adapter-specific command can open the local database, apply migrations, and
invoke the runner:

```python
connection = connect_database(settings.database_path)
apply_migrations(connection)
report = SyncRunner(connection).run(adapter, handler)
```

That command can be called by `cron`, `launchd`, or another nightly local
scheduler. It requires no cloud service. The scheduler should treat a
`failed` report as a failed job and use the persisted `sync_run_id` for
diagnosis.
