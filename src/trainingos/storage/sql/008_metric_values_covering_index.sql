CREATE INDEX idx_metric_values_key_status_owner_val
ON metric_values (metric_key, metric_status, owner_record_id, metric_value);
