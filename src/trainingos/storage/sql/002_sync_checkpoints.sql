CREATE TABLE sync_checkpoints (
    source TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(sync_run_id)
);

ALTER TABLE sync_runs
ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1));

ALTER TABLE sync_errors
ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0);

ALTER TABLE sync_errors
ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1));
