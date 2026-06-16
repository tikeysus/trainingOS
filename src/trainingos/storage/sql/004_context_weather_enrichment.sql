CREATE TABLE activity_weather_observations (
    activity_id TEXT NOT NULL REFERENCES activities(record_id) ON DELETE CASCADE,
    weather_observation_id TEXT NOT NULL REFERENCES weather_observations(record_id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (activity_id, weather_observation_id)
);

CREATE INDEX idx_context_notes_occurred_at
ON context_notes (occurred_at);

CREATE INDEX idx_context_notes_kind_occurred_at
ON context_notes (note_kind, occurred_at);

CREATE INDEX idx_weather_observations_observed_at
ON weather_observations (observed_at);

CREATE INDEX idx_activity_weather_activity
ON activity_weather_observations (activity_id);
