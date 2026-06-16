CREATE TABLE retrieval_documents (
    document_id TEXT PRIMARY KEY,
    document_type TEXT NOT NULL CHECK (
        document_type IN (
            'activity',
            'workout',
            'week',
            'note',
            'race',
            'training_block'
        )
    ),
    source_record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    source_updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    caveats_json TEXT NOT NULL DEFAULT '[]',
    document_version TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    stale_reason TEXT,
    UNIQUE (document_type, source_record_id)
);

CREATE INDEX idx_retrieval_documents_type
ON retrieval_documents (document_type);

CREATE INDEX idx_retrieval_documents_source
ON retrieval_documents (source_record_id);

CREATE INDEX idx_retrieval_documents_stale
ON retrieval_documents (stale_reason);

CREATE VIRTUAL TABLE retrieval_document_fts USING fts5(
    document_id UNINDEXED,
    title,
    body
);
