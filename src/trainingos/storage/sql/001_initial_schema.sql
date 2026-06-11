CREATE TABLE sync_runs (
    sync_run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cursor_start TEXT,
    cursor_end TEXT,
    imported_count INTEGER NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    CHECK (
        (status = 'running' AND finished_at IS NULL)
        OR (status != 'running' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX idx_sync_runs_source_started_at
ON sync_runs (source, started_at);

CREATE TABLE sync_errors (
    sync_error_id INTEGER PRIMARY KEY,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(sync_run_id) ON DELETE CASCADE,
    external_id TEXT,
    error_code TEXT NOT NULL,
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX idx_sync_errors_sync_run_id
ON sync_errors (sync_run_id);

CREATE TABLE raw_source_records (
    raw_record_id TEXT PRIMARY KEY,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(sync_run_id),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    content_type TEXT NOT NULL,
    checksum TEXT NOT NULL,
    storage_path TEXT,
    payload BLOB,
    source_updated_at TEXT,
    ingested_at TEXT NOT NULL,
    CHECK (storage_path IS NOT NULL OR payload IS NOT NULL),
    UNIQUE (source, external_id, checksum)
);

CREATE INDEX idx_raw_source_records_source_external_id
ON raw_source_records (source, external_id);

CREATE TABLE records (
    record_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provenance_kind TEXT CHECK (
        provenance_kind IS NULL
        OR provenance_kind IN (
            'observed',
            'imported',
            'user_entered',
            'computed',
            'model_interpreted'
        )
    ),
    method_name TEXT,
    method_version TEXT,
    CHECK (
        (provenance_kind = 'computed' AND method_name IS NOT NULL AND method_version IS NOT NULL)
        OR (provenance_kind != 'computed' AND method_name IS NULL AND method_version IS NULL)
        OR provenance_kind IS NULL
    )
);

CREATE INDEX idx_records_type_updated_at
ON records (record_type, updated_at);

CREATE TABLE source_references (
    source_reference_id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    raw_record_id TEXT REFERENCES raw_source_records(raw_record_id),
    UNIQUE (record_id, source, external_id)
);

CREATE INDEX idx_source_references_source_external_id
ON source_references (source, external_id);

CREATE TABLE provenance_evidence (
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    evidence_record_id TEXT NOT NULL REFERENCES records(record_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (record_id, evidence_record_id),
    UNIQUE (record_id, position)
);

CREATE TABLE provenance_caveats (
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    caveat TEXT NOT NULL,
    PRIMARY KEY (record_id, position)
);

CREATE TABLE activities (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    activity_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    distance_metres REAL CHECK (distance_metres >= 0),
    title TEXT
);

CREATE INDEX idx_activities_started_at
ON activities (started_at);

CREATE TABLE laps (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    activity_id TEXT NOT NULL REFERENCES activities(record_id) ON DELETE CASCADE,
    lap_index INTEGER NOT NULL CHECK (lap_index >= 0),
    started_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    distance_metres REAL CHECK (distance_metres >= 0),
    UNIQUE (activity_id, lap_index)
);

CREATE TABLE activity_samples (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    activity_id TEXT NOT NULL REFERENCES activities(record_id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    UNIQUE (activity_id, recorded_at)
);

CREATE INDEX idx_activity_samples_activity_recorded_at
ON activity_samples (activity_id, recorded_at);

CREATE TABLE daily_health (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    local_date TEXT NOT NULL
);

CREATE INDEX idx_daily_health_local_date
ON daily_health (local_date);

CREATE TABLE context_notes (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    note_kind TEXT NOT NULL,
    note_text TEXT NOT NULL
);

CREATE TABLE context_note_links (
    note_id TEXT NOT NULL REFERENCES context_notes(record_id) ON DELETE CASCADE,
    linked_record_id TEXT NOT NULL REFERENCES records(record_id),
    PRIMARY KEY (note_id, linked_record_id)
);

CREATE TABLE weather_observations (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    condition TEXT
);

CREATE TABLE races (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    distance_metres REAL NOT NULL CHECK (distance_metres > 0),
    result_duration_seconds REAL CHECK (result_duration_seconds > 0),
    target_duration_seconds REAL CHECK (target_duration_seconds > 0)
);

CREATE TABLE training_blocks (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    target_race_id TEXT REFERENCES races(record_id),
    CHECK (end_date >= start_date)
);

CREATE TABLE metric_evidence (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    metric_key TEXT NOT NULL,
    metric_value REAL NOT NULL,
    unit TEXT NOT NULL,
    quality REAL CHECK (quality BETWEEN 0.0 AND 1.0),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    summary TEXT,
    CHECK (window_end >= window_start)
);

CREATE INDEX idx_metric_evidence_key_window
ON metric_evidence (metric_key, window_start, window_end);

CREATE TABLE metric_values (
    metric_value_id INTEGER PRIMARY KEY,
    owner_record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    metric_key TEXT NOT NULL,
    metric_value REAL NOT NULL,
    unit TEXT NOT NULL,
    quality REAL CHECK (quality BETWEEN 0.0 AND 1.0),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (owner_record_id, metric_key)
);
