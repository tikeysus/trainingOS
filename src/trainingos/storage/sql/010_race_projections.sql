CREATE TABLE race_projections (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    target_race_id TEXT NOT NULL REFERENCES races(record_id),
    status TEXT NOT NULL CHECK (status IN ('estimated', 'insufficient_data')),
    projected_duration_seconds REAL,
    uncertainty_seconds REAL,
    CHECK (
        (status = 'estimated'
            AND projected_duration_seconds > 0
            AND uncertainty_seconds >= 0)
        OR (status = 'insufficient_data'
            AND projected_duration_seconds IS NULL
            AND uncertainty_seconds IS NULL)
    )
);

CREATE INDEX idx_race_projections_target_race_id
ON race_projections (target_race_id);
