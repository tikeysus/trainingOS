CREATE VIEW dashboard_weekly_training AS
SELECT
    summary.record_id,
    summary.week_start_date,
    summary.week_end_date,
    summary.timezone,
    summary.run_count,
    summary.distance_metres,
    summary.distance_metres / 1000.0 AS distance_kilometres,
    summary.duration_seconds,
    summary.long_run_metres,
    CASE
        WHEN summary.long_run_metres IS NULL THEN NULL
        ELSE summary.long_run_metres / 1000.0
    END AS long_run_kilometres,
    summary.average_pace_seconds_per_kilometre,
    record.method_name,
    record.method_version,
    record.updated_at AS computed_at,
    summary.data_gaps_json,
    (
        SELECT group_concat(caveat.caveat, '; ')
        FROM provenance_caveats AS caveat
        WHERE caveat.record_id = summary.record_id
        ORDER BY caveat.position
    ) AS caveats
FROM weekly_summaries AS summary
JOIN records AS record ON record.record_id = summary.record_id;

CREATE VIEW dashboard_metric_timeseries AS
SELECT
    metric.record_id,
    metric.metric_key,
    metric.metric_value,
    metric.unit,
    metric.quality,
    metric.window_start,
    metric.window_end,
    metric.summary,
    record.timezone,
    record.method_name,
    record.method_version,
    record.updated_at AS computed_at,
    (
        SELECT group_concat(caveat.caveat, '; ')
        FROM provenance_caveats AS caveat
        WHERE caveat.record_id = metric.record_id
        ORDER BY caveat.position
    ) AS caveats,
    (
        SELECT group_concat(evidence.evidence_record_id, ',')
        FROM provenance_evidence AS evidence
        WHERE evidence.record_id = metric.record_id
        ORDER BY evidence.position
    ) AS evidence_record_ids
FROM metric_evidence AS metric
JOIN records AS record ON record.record_id = metric.record_id;

CREATE VIEW dashboard_block_comparison AS
WITH block_weeks AS (
    SELECT
        block.record_id AS block_id,
        block.name AS block_name,
        block.start_date,
        block.end_date,
        block.target_race_id,
        race.name AS target_race_name,
        week.record_id AS weekly_summary_id,
        week.week_start_date,
        week.run_count,
        week.distance_metres,
        week.duration_seconds,
        week.long_run_metres,
        week.data_gaps_json
    FROM training_blocks AS block
    LEFT JOIN races AS race ON race.record_id = block.target_race_id
    LEFT JOIN weekly_summaries AS week
      ON week.week_start_date >= block.start_date
     AND week.week_start_date <= block.end_date
),
block_totals AS (
    SELECT
        block_id,
        block_name,
        start_date,
        end_date,
        target_race_id,
        target_race_name,
        COUNT(weekly_summary_id) AS week_count,
        COALESCE(SUM(run_count), 0) AS run_count,
        COALESCE(SUM(distance_metres), 0.0) AS distance_metres,
        COALESCE(SUM(duration_seconds), 0.0) AS duration_seconds,
        MAX(long_run_metres) AS longest_run_metres,
        MAX(CASE WHEN weekly_summary_id IS NULL THEN 1 ELSE 0 END) AS missing_weekly_data
    FROM block_weeks
    GROUP BY block_id, block_name, start_date, end_date, target_race_id,
             target_race_name
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
    current.target_race_id AS current_target_race_id,
    current.target_race_name AS current_target_race_name,
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

CREATE VIEW dashboard_evidence_documents AS
SELECT
    document.document_id,
    document.document_type,
    document.source_record_id,
    document.source_updated_at,
    document.title,
    document.body,
    document.metadata_json,
    document.evidence_json,
    document.caveats_json,
    document.document_version,
    document.generated_at,
    document.stale_reason,
    record.record_type AS source_record_type,
    record.timezone AS source_timezone,
    record.updated_at AS source_record_updated_at
FROM retrieval_documents AS document
JOIN records AS record ON record.record_id = document.source_record_id;

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
    block.name AS label,
    'Training block starts' AS text,
    record.timezone AS timezone,
    block.target_race_id AS linked_record_ids
FROM training_blocks AS block
JOIN records AS record ON record.record_id = block.record_id;

CREATE VIEW dashboard_race_projection_status AS
SELECT
    race.record_id AS race_id,
    race.name AS race_name,
    race.started_at,
    race.distance_metres,
    race.target_duration_seconds,
    'unavailable' AS projection_status,
    NULL AS projected_duration_seconds,
    NULL AS uncertainty_seconds,
    'race projection evidence has not been persisted yet' AS caveat,
    record.timezone
FROM races AS race
JOIN records AS record ON record.record_id = race.record_id;
