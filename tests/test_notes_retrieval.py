from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trainingos.domain import (
    ContextNote,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
)
from trainingos.normalization import NormalizationStore
from trainingos.retrieval import generate_retrieval_documents, search_retrieval_documents
from trainingos.storage import apply_migrations, connect_database


_T0 = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
_T1 = datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
_T2 = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


def _make_note(
    *,
    record_id: str,
    kind: NoteKind = NoteKind.GENERAL,
    text: str,
    occurred_at: datetime = _T0,
    linked_record_ids: tuple[str, ...] = (),
    updated_at: datetime = _T1,
) -> ContextNote:
    return ContextNote(
        metadata=RecordMetadata(
            record_id=record_id,
            timezone="America/Toronto",
            created_at=_T0,
            updated_at=updated_at,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        ),
        occurred_at=occurred_at,
        kind=kind,
        text=text,
        linked_record_ids=linked_record_ids,
    )


def _seed_activity(connection, activity_id: str = "activity-ret-1") -> str:
    connection.execute(
        """
        INSERT INTO sync_runs (sync_run_id, source, status, started_at, finished_at)
        VALUES ('run-ret-1', 'fixture', 'completed',
                '2026-07-01T06:00:00+00:00', '2026-07-01T06:01:00+00:00')
        """
    )
    connection.execute(
        f"""
        INSERT INTO records (record_id, record_type, timezone, created_at, updated_at,
                             provenance_kind)
        VALUES ('{activity_id}', 'activity', 'America/Toronto',
                '2026-07-01T06:00:00+00:00', '2026-07-01T06:00:00+00:00',
                'imported')
        """
    )
    connection.execute(
        f"""
        INSERT INTO activities (record_id, activity_type, started_at,
                                duration_seconds, distance_metres)
        VALUES ('{activity_id}', 'run', '2026-07-01T05:30:00+00:00', 3600, 10000)
        """
    )
    connection.commit()
    return activity_id


class NormalizationStoreRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    # F. State-Based / Idempotency

    def test_upsert_note_twice_same_record_id_keeps_latest(self) -> None:
        first = _make_note(record_id="note-idem-1", text="First body", updated_at=_T1)
        second = _make_note(record_id="note-idem-1", text="Updated body", updated_at=_T2)

        self.store.upsert_context_note(first)
        self.store.upsert_context_note(second)

        count = self.connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
        text = self.connection.execute(
            "SELECT note_text FROM context_notes WHERE record_id = 'note-idem-1'"
        ).fetchone()["note_text"]
        self.assertEqual(1, count)
        self.assertEqual("Updated body", text)

    def test_upsert_note_with_older_updated_at_does_not_overwrite(self) -> None:
        newer = _make_note(record_id="note-stale-1", text="Newer body", updated_at=_T2)
        older = _make_note(record_id="note-stale-1", text="Older body", updated_at=_T1)

        self.store.upsert_context_note(newer)
        self.store.upsert_context_note(older)

        text = self.connection.execute(
            "SELECT note_text FROM context_notes WHERE record_id = 'note-stale-1'"
        ).fetchone()["note_text"]
        self.assertEqual("Newer body", text)

    def test_upsert_note_updates_links_on_second_upsert(self) -> None:
        activity_id = _seed_activity(self.connection)
        without_link = _make_note(record_id="note-link-1", text="No link", updated_at=_T1)
        with_link = _make_note(
            record_id="note-link-1",
            text="With link",
            linked_record_ids=(activity_id,),
            updated_at=_T2,
        )

        self.store.upsert_context_note(without_link)
        self.store.upsert_context_note(with_link)

        count = self.connection.execute(
            "SELECT COUNT(*) FROM context_note_links WHERE note_id = 'note-link-1'"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_upsert_note_removes_links_when_second_upsert_has_none(self) -> None:
        activity_id = _seed_activity(self.connection)
        with_link = _make_note(
            record_id="note-unlink-1",
            text="Had link",
            linked_record_ids=(activity_id,),
            updated_at=_T1,
        )
        without_link = _make_note(
            record_id="note-unlink-1",
            text="No link now",
            linked_record_ids=(),
            updated_at=_T2,
        )

        self.store.upsert_context_note(with_link)
        self.store.upsert_context_note(without_link)

        count = self.connection.execute(
            "SELECT COUNT(*) FROM context_note_links WHERE note_id = 'note-unlink-1'"
        ).fetchone()[0]
        self.assertEqual(0, count)


class NoteRetrievalDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def _generate(self):
        return generate_retrieval_documents(self.connection)

    def _search(self, query: str, document_type: str = "note"):
        return search_retrieval_documents(
            self.connection,
            query,
            document_types=(document_type,),
        )

    # A. Happy Path — one test per note kind

    def test_illness_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(
            _make_note(record_id="note-ill-1", kind=NoteKind.ILLNESS, text="Severe flu")
        )
        self._generate()

        results = self._search("flu illness")
        bodies = [r.document.body for r in results]
        self.assertTrue(
            any("Severe flu" in b for b in bodies),
            f"Expected note text in retrieval documents, got bodies: {bodies}",
        )
        types = [r.document.document_type for r in results]
        self.assertIn("note", types)

    def test_injury_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(
            _make_note(record_id="note-inj-1", kind=NoteKind.INJURY, text="Achilles tendinopathy")
        )
        self._generate()

        results = self._search("Achilles tendinopathy")
        self.assertTrue(any("Achilles tendinopathy" in r.document.body for r in results))

    def test_travel_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(
            _make_note(record_id="note-trv-1", kind=NoteKind.TRAVEL, text="Flight to Boston for race")
        )
        self._generate()

        results = self._search("Boston travel")
        self.assertTrue(any("Boston" in r.document.body for r in results))

    def test_stress_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(
            _make_note(record_id="note-str-1", kind=NoteKind.STRESS, text="High work stress this week")
        )
        self._generate()

        results = self._search("stress work")
        self.assertTrue(any("stress" in r.document.body.lower() for r in results))

    def test_general_note_appears_in_retrieval_documents(self) -> None:
        self.store.upsert_context_note(
            _make_note(record_id="note-gen-1", kind=NoteKind.GENERAL, text="Legs feeling heavy")
        )
        self._generate()

        results = self._search("legs heavy")
        self.assertTrue(any("Legs feeling heavy" in r.document.body for r in results))

    def test_note_retrieval_document_includes_linked_activity_id(self) -> None:
        activity_id = _seed_activity(self.connection)
        self.store.upsert_context_note(
            _make_note(
                record_id="note-linked-1",
                kind=NoteKind.INJURY,
                text="Post-race soreness",
                linked_record_ids=(activity_id,),
            )
        )
        self._generate()

        results = self._search("soreness")
        self.assertTrue(len(results) > 0)
        doc = results[0].document
        # Activity ID should appear in evidence or body
        evidence_or_body = doc.body + str(doc.evidence_record_ids)
        self.assertIn(activity_id, evidence_or_body)

    # F. State-Based

    def test_refresh_after_new_note_increases_generated_count(self) -> None:
        report_before = self._generate()

        self.store.upsert_context_note(
            _make_note(record_id="note-new-1", text="Brand new note")
        )
        report_after = self._generate()

        self.assertEqual(
            report_before.generated_count + 1,
            report_after.generated_count,
        )

    def test_refresh_after_note_deleted_marks_retrieval_document_stale_or_deletes_it(self) -> None:
        self.store.upsert_context_note(
            _make_note(record_id="note-del-1", text="Note to be deleted")
        )
        report_after_add = self._generate()
        self.assertEqual(1, report_after_add.generated_count)

        # Delete the note from both tables
        self.connection.execute(
            "DELETE FROM context_note_links WHERE note_id = 'note-del-1'"
        )
        self.connection.execute(
            "DELETE FROM context_notes WHERE record_id = 'note-del-1'"
        )
        self.connection.execute(
            "DELETE FROM records WHERE record_id = 'note-del-1'"
        )
        self.connection.commit()

        report_after_delete = self._generate()

        # The note document should either be deleted or marked stale
        self.assertTrue(
            report_after_delete.deleted_count > 0 or report_after_delete.stale_count > 0,
            f"Expected deleted or stale documents after note removal, got: {report_after_delete}",
        )

    # Retrieval search integration

    def test_illness_notes_surface_for_relevant_coach_query(self) -> None:
        self.store.upsert_context_note(
            _make_note(
                record_id="note-coach-1",
                kind=NoteKind.ILLNESS,
                text="Achilles tendinopathy flare-up after long run",
            )
        )
        self._generate()

        results = search_retrieval_documents(self.connection, "injury recovery")
        self.assertTrue(
            any(r.document.document_type == "note" for r in results),
            "Expected at least one note-type document in results for 'injury recovery' query",
        )


if __name__ == "__main__":
    unittest.main()
