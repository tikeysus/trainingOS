-- Remove ON DELETE CASCADE from retrieval_documents.source_record_id so that
-- deleting a source record does not silently cascade-delete its retrieval document.
-- generate_retrieval_documents() is responsible for cleanup via expected vs existing ID diffing.

CREATE TABLE retrieval_documents_new (
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
    source_record_id TEXT NOT NULL,
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

INSERT INTO retrieval_documents_new SELECT * FROM retrieval_documents;

DROP TABLE retrieval_documents;

ALTER TABLE retrieval_documents_new RENAME TO retrieval_documents;

CREATE INDEX idx_retrieval_documents_type
ON retrieval_documents (document_type);

CREATE INDEX idx_retrieval_documents_source
ON retrieval_documents (source_record_id);

CREATE INDEX idx_retrieval_documents_stale
ON retrieval_documents (stale_reason);
