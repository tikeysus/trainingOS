CREATE UNIQUE INDEX idx_source_references_source_external_id_unique
ON source_references (source, external_id);

ALTER TABLE source_references
ADD COLUMN parser_name TEXT;

ALTER TABLE source_references
ADD COLUMN parser_version TEXT;

ALTER TABLE metric_values
RENAME TO metric_values_legacy;

CREATE TABLE metric_values (
    metric_value_id INTEGER PRIMARY KEY,
    owner_record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    metric_key TEXT NOT NULL,
    metric_status TEXT NOT NULL CHECK (
        metric_status IN ('measured', 'unavailable', 'unsupported')
    ),
    metric_value REAL,
    unit TEXT,
    quality REAL CHECK (quality BETWEEN 0.0 AND 1.0),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    CHECK (
        (metric_status = 'measured' AND metric_value IS NOT NULL AND unit IS NOT NULL)
        OR
        (metric_status != 'measured' AND metric_value IS NULL AND unit IS NULL
         AND quality IS NULL)
    ),
    UNIQUE (owner_record_id, metric_key)
);

INSERT INTO metric_values (
    metric_value_id, owner_record_id, metric_key, metric_status, metric_value,
    unit, quality, attributes_json
)
SELECT
    metric_value_id, owner_record_id, metric_key, 'measured', metric_value,
    unit, quality, attributes_json
FROM metric_values_legacy;

DROP TABLE metric_values_legacy;
