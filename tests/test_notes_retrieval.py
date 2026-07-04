from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trainingos.domain import (
    Activity,
    ActivityType,
    ContextNote,
    Measurement,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.refresh import refresh_training_data
from trainingos.retrieval import generate_retrieval_documents
from trainingos.storage import apply_migrations, connect_database


class NoteRetrievalDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.connection = connect_database(
            Path(self.temporary_directory.name) / "training.sqlite3"
        )
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_illness_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.ILLNESS, "Flu, missed tempo run"))
        self.connection.commit()

        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))

        row = self._document("note", "note-1")
        self.assertIsNotNone(row)
        self.assertIn("illness", row["body"].lower())

    def test_injury_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.INJURY, "Left calf strain"))
        self.connection.commit()

        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))

        row = self._document("note", "note-1")
        self.assertIsNotNone(row)
        self.assertIn("injury", row["body"].lower())

    def test_travel_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.TRAVEL, "Boston business trip"))
        self.connection.commit()

        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))

        row = self._document("note", "note-1")
        self.assertIsNotNone(row)
        self.assertIn("travel", row["body"].lower())

    def test_note_document_body_contains_note_text(self) -> None:
        self.store.upsert_context_note(
            self._note("note-1", NoteKind.INJURY, "Right knee swelling, skipping tempo")
        )
        self.connection.commit()

        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))

        row = self._document("note", "note-1")
        self.assertIn("Right knee swelling, skipping tempo", row["body"])

    def test_note_document_linked_to_activity_includes_activity_in_evidence(self) -> None:
        self._seed_activity("activity-1")
        self.store.upsert_context_note(
            self._note("note-1", NoteKind.INJURY, "Calf tightness", linked_record_ids=("activity-1",))
        )
        self.connection.commit()

        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))

        row = self._document("note", "note-1")
        self.assertIn("activity-1", row["evidence_json"])

    def test_upsert_context_note_is_idempotent(self) -> None:
        note = self._note("note-1", NoteKind.ILLNESS, "Fever")
        self.store.upsert_context_note(note)
        self.store.upsert_context_note(note)
        self.connection.commit()

        count = self.connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
        self.assertEqual(1, count)

    def test_updated_note_text_appears_in_regenerated_document(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.ILLNESS, "Original text"))
        self.connection.commit()
        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))

        self.store.upsert_context_note(
            self._note(
                "note-1",
                NoteKind.ILLNESS,
                "Updated text after rest",
                updated_at=datetime(2026, 11, 9, 14, 0, tzinfo=UTC),
            )
        )
        self.connection.commit()
        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 15, 0, tzinfo=UTC))

        row = self._document("note", "note-1")
        self.assertIn("Updated text after rest", row["body"])
        self.assertNotIn("Original text", row["body"])

    def test_deleted_note_record_removes_retrieval_document_on_next_generation(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.INJURY, "Temporary note"))
        self.connection.commit()
        generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
        self.assertIsNotNone(self._document("note", "note-1"))

        self.connection.execute("DELETE FROM records WHERE record_id = 'note-1'")
        self.connection.commit()
        report = generate_retrieval_documents(self.connection, now=datetime(2026, 11, 9, 14, 0, tzinfo=UTC))

        self.assertIsNone(self._optional_document("note", "note-1"))
        self.assertEqual(0, report.deleted_count)

    def test_refresh_includes_note_document_in_dashboard_evidence(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.ILLNESS, "Sick day"))
        self.connection.commit()

        refresh_training_data(
            self.connection,
            timezone="America/Toronto",
            now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        )

        count = self.connection.execute(
            "SELECT COUNT(*) FROM dashboard_evidence_documents WHERE document_type = 'note'"
        ).fetchone()[0]
        self.assertGreater(count, 0)

    def test_refresh_regenerates_note_document_after_update(self) -> None:
        self.store.upsert_context_note(self._note("note-1", NoteKind.INJURY, "Original body text"))
        self.connection.commit()
        refresh_training_data(
            self.connection,
            timezone="America/Toronto",
            now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        )

        self.store.upsert_context_note(
            self._note(
                "note-1",
                NoteKind.INJURY,
                "Revised body text after assessment",
                updated_at=datetime(2026, 11, 9, 14, 0, tzinfo=UTC),
            )
        )
        self.connection.commit()
        refresh_training_data(
            self.connection,
            timezone="America/Toronto",
            now=datetime(2026, 11, 9, 15, 0, tzinfo=UTC),
        )

        row = self._document("note", "note-1")
        self.assertIn("Revised body text after assessment", row["body"])

    def _note(
        self,
        record_id: str,
        kind: NoteKind,
        text: str,
        *,
        linked_record_ids: tuple[str, ...] = (),
        updated_at: datetime | None = None,
    ) -> ContextNote:
        timestamp = updated_at or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        return ContextNote(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
                updated_at=timestamp,
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            occurred_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
            kind=kind,
            text=text,
            linked_record_ids=linked_record_ids,
        )

    def _seed_activity(self, record_id: str) -> None:
        timestamp = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        self.store.upsert_activity(
            Activity(
                metadata=RecordMetadata(
                    record_id=record_id,
                    timezone="America/Toronto",
                    provenance=Provenance(ProvenanceKind.USER_ENTERED),
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                activity_type=ActivityType.RUN,
                started_at=datetime(2026, 11, 2, 11, 0, tzinfo=UTC),
                duration=Measurement(3600, Unit.SECOND),
                distance=Measurement(10000, Unit.METRE),
            )
        )
        self.connection.commit()

    def _document(self, document_type: str, source_record_id: str):
        row = self._optional_document(document_type, source_record_id)
        self.assertIsNotNone(row, f"No {document_type!r} document for {source_record_id!r}")
        return row

    def _optional_document(self, document_type: str, source_record_id: str):
        return self.connection.execute(
            """
            SELECT *
            FROM retrieval_documents
            WHERE document_type = ? AND source_record_id = ?
            """,
            (document_type, source_record_id),
        ).fetchone()


if __name__ == "__main__":
    unittest.main()
