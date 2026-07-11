ALTER TABLE training_blocks RENAME TO training_blocks_old;

CREATE TABLE training_blocks (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    start_date TEXT NOT NULL,
    race_date TEXT,
    ended_at TEXT
);

INSERT INTO training_blocks (record_id, goal, start_date)
SELECT record_id, name, start_date FROM training_blocks_old;

DROP TABLE training_blocks_old;

CREATE TABLE block_phase_transitions (
    id INTEGER PRIMARY KEY,
    block_id TEXT NOT NULL REFERENCES training_blocks(record_id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK (phase IN ('base', 'build', 'peak', 'taper')),
    recorded_at TEXT NOT NULL
);

CREATE INDEX idx_block_phase_transitions_block_id
ON block_phase_transitions (block_id);
