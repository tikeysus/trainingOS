DROP VIEW IF EXISTS dashboard_context_annotations;
DROP VIEW IF EXISTS dashboard_block_comparison;

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

-- Recreate dashboard views with updated column references
CREATE VIEW dashboard_block_comparison AS
WITH block_weeks AS (
    SELECT
        block.record_id AS block_id,
        block.goal AS block_name,
        block.start_date,
        COALESCE(block.race_date, DATE(block.ended_at)) AS end_date,
        week.record_id AS weekly_summary_id,
        week.week_start_date,
        week.run_count,
        week.distance_metres,
        week.duration_seconds,
        week.long_run_metres,
        week.data_gaps_json
    FROM training_blocks AS block
    LEFT JOIN weekly_summaries AS week
      ON week.week_start_date >= block.start_date
     AND week.week_start_date <= COALESCE(block.race_date, DATE(block.ended_at))
),
block_totals AS (
    SELECT
        block_id,
        block_name,
        start_date,
        end_date,
        COUNT(weekly_summary_id) AS week_count,
        COALESCE(SUM(run_count), 0) AS run_count,
        COALESCE(SUM(distance_metres), 0.0) AS distance_metres,
        COALESCE(SUM(duration_seconds), 0.0) AS duration_seconds,
        MAX(long_run_metres) AS longest_run_metres,
        MAX(CASE WHEN weekly_summary_id IS NULL THEN 1 ELSE 0 END) AS missing_weekly_data
    FROM block_weeks
    GROUP BY block_id, block_name, start_date, end_date
),
ranked_blocks AS (
    SELECT
        *,
        ROW_NUMBER() OVER (ORDER BY start_date DESC, block_id DESC) AS recency_rank
    FROM block_totals
)
SELECT
    current.block_id AS current_block_id,
    current.block_name AS current_block_name,
    current.start_date AS current_start_date,
    current.end_date AS current_end_date,
    current.week_count AS current_week_count,
    current.run_count AS current_run_count,
    current.distance_metres AS current_distance_metres,
    current.distance_metres / 1000.0 AS current_distance_kilometres,
    current.duration_seconds AS current_duration_seconds,
    current.longest_run_metres AS current_longest_run_metres,
    previous.block_id AS previous_block_id,
    previous.block_name AS previous_block_name,
    previous.start_date AS previous_start_date,
    previous.end_date AS previous_end_date,
    previous.week_count AS previous_week_count,
    previous.run_count AS previous_run_count,
    previous.distance_metres AS previous_distance_metres,
    previous.distance_metres / 1000.0 AS previous_distance_kilometres,
    previous.duration_seconds AS previous_duration_seconds,
    previous.longest_run_metres AS previous_longest_run_metres,
    current.distance_metres - previous.distance_metres AS distance_delta_metres,
    current.run_count - previous.run_count AS run_count_delta,
    CASE
        WHEN previous.block_id IS NULL THEN 'prior_block_missing'
        WHEN current.missing_weekly_data = 1 THEN 'current_block_missing_weekly_data'
        WHEN previous.missing_weekly_data = 1 THEN 'prior_block_missing_weekly_data'
        ELSE NULL
    END AS caveat
FROM ranked_blocks AS current
LEFT JOIN ranked_blocks AS previous
  ON previous.recency_rank = current.recency_rank + 1
WHERE current.recency_rank = 1;

CREATE VIEW dashboard_context_annotations AS
SELECT
    note.record_id AS annotation_id,
    'context_note' AS annotation_type,
    note.occurred_at AS occurred_at,
    note.note_kind AS label,
    note.note_text AS text,
    record.timezone AS timezone,
    (
        SELECT group_concat(link.linked_record_id, ',')
        FROM context_note_links AS link
        WHERE link.note_id = note.record_id
        ORDER BY link.linked_record_id
    ) AS linked_record_ids
FROM context_notes AS note
JOIN records AS record ON record.record_id = note.record_id
UNION ALL
SELECT
    race.record_id AS annotation_id,
    'race' AS annotation_type,
    race.started_at AS occurred_at,
    race.name AS label,
    CASE
        WHEN race.result_duration_seconds IS NULL THEN 'Race result missing'
        ELSE 'Race result available'
    END AS text,
    record.timezone AS timezone,
    NULL AS linked_record_ids
FROM races AS race
JOIN records AS record ON record.record_id = race.record_id
UNION ALL
SELECT
    block.record_id AS annotation_id,
    'training_block' AS annotation_type,
    block.start_date || 'T00:00:00+00:00' AS occurred_at,
    block.goal AS label,
    'Training block starts' AS text,
    record.timezone AS timezone,
    NULL AS linked_record_ids
FROM training_blocks AS block
JOIN records AS record ON record.record_id = block.record_id;
