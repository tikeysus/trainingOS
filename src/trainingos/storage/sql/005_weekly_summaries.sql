CREATE TABLE weekly_summaries (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    week_start_date TEXT NOT NULL,
    week_end_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    run_count INTEGER NOT NULL CHECK (run_count >= 0),
    distance_metres REAL NOT NULL CHECK (distance_metres >= 0),
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    long_run_metres REAL,
    average_pace_seconds_per_kilometre REAL,
    data_gaps_json TEXT NOT NULL DEFAULT '[]',
    CHECK (week_end_date >= week_start_date),
    CHECK (long_run_metres IS NULL OR long_run_metres >= 0),
    CHECK (
        average_pace_seconds_per_kilometre IS NULL
        OR average_pace_seconds_per_kilometre > 0
    ),
    UNIQUE (timezone, week_start_date)
);

CREATE INDEX idx_weekly_summaries_week_start
ON weekly_summaries (week_start_date);
