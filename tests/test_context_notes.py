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
from trainingos.retrieval import _note_documents
from trainingos.storage import apply_migrations, connect_database


class ContextNoteRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    # ── A. Happy Path ──────────────────────────────────────────────────────────

    def test_insert_and_retrieve_round_trip(self) -> None:
        self.store.upsert_context_note(
            self._note("note-1", NoteKind.ILLNESS, "Flu symptoms, rest day.")
        )
        self.connection.commit()

        row = self.connection.execute(
            "SELECT note_kind, note_text FROM context_notes WHERE record_id = 'note-1'"
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual("illness", row["note_kind"])
        self.assertEqual("Flu symptoms, rest day.", row["note_text"])

    def test_note_with_linked_activity_stored_in_context_note_links(self) -> None:
        self.store.upsert_activity(self._activity("activity-link-target"))
        self.store.upsert_context_note(
            self._note(
                "note-linked",
                NoteKind.INJURY,
                "Knee pain post-run.",
                linked_record_ids=("activity-link-target",),
            )
        )
        self.connection.commit()

        links = self.connection.execute(
            "SELECT linked_record_id FROM context_note_links WHERE note_id = 'note-linked'"
        ).fetchall()

        self.assertEqual(["activity-link-target"], [r["linked_record_id"] for r in links])

    def test_note_document_generated_for_illness_note(self) -> None:
        self.store.upsert_context_note(
            self._note("note-illness", NoteKind.ILLNESS, "Heavy cold, skipped long run.")
        )
        self.connection.commit()

        documents = list(_note_documents(self.connection))

        self.assertEqual(1, len(documents))
        doc = documents[0]
        self.assertEqual("note", doc.document_type)
        self.assertIn("illness", doc.body)
        self.assertIn("Heavy cold, skipped long run.", doc.body)
        self.assertEqual("note-illness", doc.source_record_id)

    def test_note_documents_generated_for_all_five_note_kinds(self) -> None:
        kinds = [
            (NoteKind.ILLNESS, "Flu"),
            (NoteKind.INJURY, "Shin splints"),
            (NoteKind.TRAVEL, "Business trip"),
            (NoteKind.STRESS, "Work deadline"),
            (NoteKind.GENERAL, "Easy day note"),
        ]
        for i, (kind, text) in enumerate(kinds):
            self.store.upsert_context_note(self._note(f"note-kind-{i}", kind, text))
        self.connection.commit()

        documents = list(_note_documents(self.connection))
        bodies = [doc.body for doc in documents]

        self.assertEqual(5, len(documents))
        self.assertTrue(any("illness" in body for body in bodies))
        self.assertTrue(any("injury" in body for body in bodies))
        self.assertTrue(any("travel" in body for body in bodies))
        self.assertTrue(any("stress" in body for body in bodies))
        self.assertTrue(any("general" in body for body in bodies))

    # ── B. Boundary Values ─────────────────────────────────────────────────────

    def test_note_with_no_linked_records_generates_document_without_linked_section(
        self,
    ) -> None:
        self.store.upsert_context_note(
            self._note("note-unlinked", NoteKind.STRESS, "Unlinked note.", linked_record_ids=())
        )
        self.connection.commit()

        documents = list(_note_documents(self.connection))

        self.assertEqual(1, len(documents))
        self.assertNotIn("Linked records", documents[0].body)

    def test_note_with_three_linked_records_lists_all_in_evidence(self) -> None:
        for activity_id in ("act-a", "act-b", "act-c"):
            self.store.upsert_activity(self._activity(activity_id))
        self.store.upsert_context_note(
            self._note(
                "note-multi",
                NoteKind.GENERAL,
                "Connected to three activities.",
                linked_record_ids=("act-a", "act-b", "act-c"),
            )
        )
        self.connection.commit()

        documents = list(_note_documents(self.connection))

        self.assertEqual(1, len(documents))
        evidence = documents[0].evidence_record_ids
        self.assertIn("act-a", evidence)
        self.assertIn("act-b", evidence)
        self.assertIn("act-c", evidence)

    # ── C. Equivalence Partitioning ────────────────────────────────────────────

    def test_multiple_illness_notes_all_appear_in_note_documents(self) -> None:
        self.store.upsert_context_note(
            self._note("note-ill-1", NoteKind.ILLNESS, "First cold.")
        )
        self.store.upsert_context_note(
            self._note("note-ill-2", NoteKind.ILLNESS, "Second cold.")
        )
        self.connection.commit()

        documents = list(_note_documents(self.connection))

        self.assertEqual(2, len(documents))
        self.assertTrue(all(doc.document_type == "note" for doc in documents))

    def test_general_and_stress_notes_appear_in_note_documents(self) -> None:
        self.store.upsert_context_note(
            self._note("note-gen", NoteKind.GENERAL, "Just a note.")
        )
        self.store.upsert_context_note(
            self._note("note-str", NoteKind.STRESS, "Work stress.")
        )
        self.connection.commit()

        documents = list(_note_documents(self.connection))

        self.assertEqual(2, len(documents))

    # ── D. Edge Cases ──────────────────────────────────────────────────────────

    def test_no_notes_in_database_returns_empty_iterable(self) -> None:
        documents = list(_note_documents(self.connection))
        self.assertEqual([], documents)

    def test_two_notes_with_identical_body_different_kinds_produce_separate_documents(
        self,
    ) -> None:
        self.store.upsert_context_note(
            self._note("note-x", NoteKind.ILLNESS, "Same text.")
        )
        self.store.upsert_context_note(
            self._note("note-y", NoteKind.STRESS, "Same text.")
        )
        self.connection.commit()

        documents = list(_note_documents(self.connection))

        self.assertEqual(2, len(documents))
        self.assertNotEqual(documents[0].document_id, documents[1].document_id)

    # ── E. Error / Negative Tests ──────────────────────────────────────────────

    def test_context_note_with_empty_text_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self._note("note-bad", NoteKind.GENERAL, "")

    def test_context_note_with_whitespace_only_text_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self._note("note-ws", NoteKind.GENERAL, "   ")

    def test_context_note_with_naive_occurred_at_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            ContextNote(
                metadata=RecordMetadata(
                    record_id="note-naive",
                    timezone="UTC",
                    created_at=datetime(2026, 7, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                    provenance=Provenance(ProvenanceKind.USER_ENTERED),
                ),
                occurred_at=datetime(2026, 7, 1),  # naive — no tzinfo
                kind=NoteKind.GENERAL,
                text="This should fail.",
            )

    # ── F. State-Based / Idempotency ──────────────────────────────────────────

    def test_re_upsert_same_record_id_with_new_text_updates_single_row(self) -> None:
        self.store.upsert_context_note(
            self._note("note-upd", NoteKind.GENERAL, "version 1")
        )
        self.store.upsert_context_note(
            self._note(
                "note-upd",
                NoteKind.GENERAL,
                "version 2",
                updated_at=datetime(2026, 7, 2, tzinfo=UTC),
            )
        )
        self.connection.commit()

        rows = self.connection.execute(
            "SELECT note_text FROM context_notes WHERE record_id = 'note-upd'"
        ).fetchall()

        self.assertEqual(1, len(rows))
        self.assertEqual("version 2", rows[0]["note_text"])

    def test_re_upsert_with_different_linked_records_clears_old_links(self) -> None:
        self.store.upsert_activity(self._activity("link-act-1"))
        self.store.upsert_activity(self._activity("link-act-2"))

        self.store.upsert_context_note(
            self._note(
                "note-relink",
                NoteKind.GENERAL,
                "First version.",
                linked_record_ids=("link-act-1",),
            )
        )
        self.store.upsert_context_note(
            self._note(
                "note-relink",
                NoteKind.GENERAL,
                "Updated version.",
                linked_record_ids=("link-act-2",),
                updated_at=datetime(2026, 7, 2, tzinfo=UTC),
            )
        )
        self.connection.commit()

        links = self.connection.execute(
            "SELECT linked_record_id FROM context_note_links WHERE note_id = 'note-relink'"
        ).fetchall()

        self.assertEqual(["link-act-2"], [r["linked_record_id"] for r in links])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _note(
        self,
        record_id: str,
        kind: NoteKind,
        text: str,
        *,
        linked_record_ids: tuple[str, ...] = (),
        updated_at: datetime | None = None,
    ) -> ContextNote:
        return ContextNote(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=updated_at or datetime(2026, 7, 1, tzinfo=UTC),
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            occurred_at=datetime(2026, 7, 1, 6, 0, tzinfo=UTC),
            kind=kind,
            text=text,
            linked_record_ids=linked_record_ids,
        )

    def _activity(self, record_id: str) -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 7, 1, 5, 0, tzinfo=UTC),
            duration=Measurement(1800, Unit.SECOND),
            distance=Measurement(5000, Unit.METRE),
        )


if __name__ == "__main__":
    unittest.main()
