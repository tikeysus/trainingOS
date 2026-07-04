DROP INDEX idx_metric_values_key_status_owner_val;

CREATE INDEX idx_metric_values_owner_key_covering
ON metric_values (owner_record_id, metric_key, metric_status, metric_value);
