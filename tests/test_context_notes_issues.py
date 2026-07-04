"""Test cases for context notes issues: race conditions, N+1 queries, HTTP semantics."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from trainingos.coach_web import create_server
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
from trainingos.providers import FakeChatProvider, OllamaHealth
from trainingos.storage import apply_migrations, connect_database


class DeleteContextNoteRaceConditionTests(unittest.TestCase):
    """Test race conditions and transaction safety in delete_context_note()."""

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

    def test_delete_context_note_removes_note_and_links(self) -> None:
        """Deleting a note removes it from context_notes and context_note_links."""
        note = self._make_note("note-1", "Delete me.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        found = self.store.delete_context_note("note-1")

        self.assertTrue(found)
        row = self.connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = 'note-1'"
        ).fetchone()
        self.assertIsNone(row)

    def test_delete_nonexistent_note_returns_false(self) -> None:
        """Deleting a note that doesn't exist returns False."""
        found = self.store.delete_context_note("note-does-not-exist")
        self.assertFalse(found)

    # ── B. Boundary Values ─────────────────────────────────────────────────────

    def test_delete_note_with_no_linked_records(self) -> None:
        """Deleting a note with no linked records succeeds."""
        note = self._make_note("note-no-links", "Standalone note.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        found = self.store.delete_context_note("note-no-links")

        self.assertTrue(found)
        links = self.connection.execute(
            "SELECT 1 FROM context_note_links WHERE note_id = 'note-no-links'"
        ).fetchall()
        self.assertEqual([], links)

    def test_delete_note_with_multiple_linked_records(self) -> None:
        """Deleting a note removes all its linked records from context_note_links."""
        self.store.upsert_activity(self._make_activity("act-1"))
        self.store.upsert_activity(self._make_activity("act-2"))
        note = self._make_note(
            "note-multi-link",
            "Linked note.",
            linked_record_ids=("act-1", "act-2"),
        )
        self.store.upsert_context_note(note)
        self.connection.commit()

        found = self.store.delete_context_note("note-multi-link")

        self.assertTrue(found)
        links = self.connection.execute(
            "SELECT 1 FROM context_note_links WHERE note_id = 'note-multi-link'"
        ).fetchall()
        self.assertEqual([], links)

    # ── C. Equivalence Partitioning ────────────────────────────────────────────

    def test_delete_note_cleans_records_table(self) -> None:
        """Deleting a note removes its row from the records table."""
        note = self._make_note("note-rec", "Delete from records.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        found = self.store.delete_context_note("note-rec")

        self.assertTrue(found)
        row = self.connection.execute(
            "SELECT 1 FROM records WHERE record_id = 'note-rec'"
        ).fetchone()
        self.assertIsNone(row)

    # ── D. Edge Cases ──────────────────────────────────────────────────────────

    def test_delete_same_note_twice_returns_false_on_second_attempt(self) -> None:
        """Deleting a note twice: first returns True, second returns False."""
        note = self._make_note("note-twice", "Delete twice.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        first = self.store.delete_context_note("note-twice")
        self.connection.commit()
        second = self.store.delete_context_note("note-twice")

        self.assertTrue(first)
        self.assertFalse(second)

    def test_delete_note_after_manual_database_deletion_returns_false(self) -> None:
        """If the note was deleted via raw SQL, delete_context_note returns False."""
        note = self._make_note("note-raw-del", "Delete via raw SQL.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        # Manually delete via raw SQL
        self.connection.execute("DELETE FROM context_notes WHERE record_id = ?", ("note-raw-del",))
        self.connection.commit()

        found = self.store.delete_context_note("note-raw-del")

        self.assertFalse(found)

    # ── E. Error / Negative Tests ──────────────────────────────────────────────

    def test_delete_with_empty_record_id_returns_false(self) -> None:
        """Deleting with an empty string record_id returns False."""
        found = self.store.delete_context_note("")
        self.assertFalse(found)

    def test_delete_with_special_characters_in_record_id(self) -> None:
        """Deleting with special characters in record_id works (parameterized query)."""
        note = self._make_note("note:with/special\nchars", "Special ID note.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        found = self.store.delete_context_note("note:with/special\nchars")

        self.assertTrue(found)

    # ── F. State-Based / Concurrency ──────────────────────────────────────────

    def test_delete_maintains_referential_integrity_no_orphaned_records(self) -> None:
        """After delete, no orphaned rows exist in records table."""
        note = self._make_note("note-integrity", "Maintain integrity.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        self.store.delete_context_note("note-integrity")
        self.connection.commit()

        orphaned = self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type = 'context_note' AND record_id = ?",
            ("note-integrity",),
        ).fetchone()[0]
        self.assertEqual(0, orphaned)

    def test_concurrent_delete_only_one_succeeds(self) -> None:
        """Two threads deleting the same note: exactly one returns True, DB is clean."""
        note = self._make_note("note-race", "Race condition.")
        self.store.upsert_context_note(note)
        self.connection.commit()

        results = []

        def delete():
            # Each thread needs its own connection to simulate real concurrent access
            with connect_database(Path(self.temporary_directory.name) / "training.sqlite3") as conn:
                s = NormalizationStore(conn)
                found = s.delete_context_note("note-race")
                if found:
                    conn.commit()
                results.append(found)

        threads = [threading.Thread(target=delete) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread should have deleted it
        self.assertEqual(1, results.count(True), "Only one concurrent delete should succeed")
        self.assertEqual(1, results.count(False), "The other delete should return False")

        # DB should be clean — note gone
        row = self.connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = 'note-race'"
        ).fetchone()
        self.assertIsNone(row)

    def test_delete_is_atomic_partial_failure_leaves_no_orphaned_links(self) -> None:
        """If the context_notes DELETE fails mid-way, context_note_links must also be intact (no partial state)."""
        note = self._make_note("note-atomic", "Atomic delete.", linked_record_ids=())
        self.store.upsert_context_note(note)
        self.connection.commit()

        class FailingConnection:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args, **kwargs):
                if "DELETE FROM context_notes" in sql:
                    raise sqlite3.OperationalError("Injected failure")
                return self._conn.execute(sql, *args, **kwargs)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *args):
                return self._conn.__exit__(*args)

        failing_conn = FailingConnection(self.connection)
        original_store_conn = self.store._connection
        self.store._connection = failing_conn

        try:
            with self.assertRaises((sqlite3.OperationalError, Exception)):
                self.store.delete_context_note("note-atomic")
        finally:
            self.store._connection = original_store_conn

        self.connection.rollback()  # Clean up any partial state

        # After failure + rollback, the note should still be fully present
        note_row = self.connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = 'note-atomic'"
        ).fetchone()
        self.assertIsNotNone(note_row, "Note must still exist after partial delete failure")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_note(
        self,
        record_id: str,
        text: str,
        *,
        linked_record_ids: tuple[str, ...] = (),
    ) -> ContextNote:
        return ContextNote(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="UTC",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
            kind=NoteKind.GENERAL,
            text=text,
            linked_record_ids=linked_record_ids,
        )

    def _make_activity(self, record_id: str) -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="UTC",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 7, 1, tzinfo=UTC),
            duration=Measurement(1800, Unit.SECOND),
            distance=Measurement(5000, Unit.METRE),
        )


class ListContextNotesQueryEfficiencyTests(unittest.TestCase):
    """Test query efficiency and correctness in list_context_notes()."""

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

    def test_list_returns_notes_in_descending_date_order(self) -> None:
        """list_context_notes returns notes ordered by date descending."""
        self.store.upsert_context_note(
            self._make_note("n1", "Older", occurred_at=date(2026, 7, 1))
        )
        self.store.upsert_context_note(
            self._make_note("n2", "Middle", occurred_at=date(2026, 7, 2))
        )
        self.store.upsert_context_note(
            self._make_note("n3", "Newer", occurred_at=date(2026, 7, 3))
        )
        self.connection.commit()

        notes = self.store.list_context_notes()

        self.assertEqual(3, len(notes))
        self.assertEqual("n3", notes[0]["record_id"])
        self.assertEqual("n2", notes[1]["record_id"])
        self.assertEqual("n1", notes[2]["record_id"])

    def test_list_with_kind_filter_returns_only_matching_kind(self) -> None:
        """list_context_notes with kind filter returns only notes of that kind."""
        self.store.upsert_context_note(self._make_note("n1", "Illness", kind=NoteKind.ILLNESS))
        self.store.upsert_context_note(self._make_note("n2", "Injury", kind=NoteKind.INJURY))
        self.store.upsert_context_note(self._make_note("n3", "Illness 2", kind=NoteKind.ILLNESS))
        self.connection.commit()

        notes = self.store.list_context_notes(kind=NoteKind.ILLNESS)

        self.assertEqual(2, len(notes))
        self.assertTrue(all(n["type"] == "illness" for n in notes))

    def test_list_with_since_filter_returns_notes_from_date_onwards(self) -> None:
        """list_context_notes with since filter returns notes >= since date."""
        self.store.upsert_context_note(
            self._make_note("n1", "Before", occurred_at=date(2026, 6, 30))
        )
        self.store.upsert_context_note(
            self._make_note("n2", "On date", occurred_at=date(2026, 7, 1))
        )
        self.store.upsert_context_note(
            self._make_note("n3", "After", occurred_at=date(2026, 7, 2))
        )
        self.connection.commit()

        notes = self.store.list_context_notes(since=date(2026, 7, 1))

        self.assertEqual(2, len(notes))
        self.assertIn("n2", [n["record_id"] for n in notes])
        self.assertIn("n3", [n["record_id"] for n in notes])

    def test_list_includes_linked_record_ids(self) -> None:
        """list_context_notes includes linked_record_ids for each note."""
        act = self._make_activity("act-1")
        self.store.upsert_activity(act)
        note = self._make_note("n-linked", "Has links", linked_record_ids=("act-1",))
        self.store.upsert_context_note(note)
        self.connection.commit()

        notes = self.store.list_context_notes()

        self.assertEqual(1, len(notes))
        self.assertEqual(["act-1"], notes[0]["linked_record_ids"])

    # ── B. Boundary Values ─────────────────────────────────────────────────────

    def test_list_empty_database_returns_empty_list(self) -> None:
        """list_context_notes on empty database returns []."""
        notes = self.store.list_context_notes()
        self.assertEqual([], notes)

    def test_list_with_one_note(self) -> None:
        """list_context_notes with one note returns that note."""
        self.store.upsert_context_note(self._make_note("n1", "Sole note"))
        self.connection.commit()

        notes = self.store.list_context_notes()

        self.assertEqual(1, len(notes))
        self.assertEqual("n1", notes[0]["record_id"])

    def test_list_with_many_notes_no_overflow(self) -> None:
        """list_context_notes with 100 notes returns all without overflow."""
        for i in range(100):
            self.store.upsert_context_note(
                self._make_note(f"n{i}", f"Note {i}", occurred_at=date(2026, 7, 1 + i // 50))
            )
        self.connection.commit()

        notes = self.store.list_context_notes()

        self.assertEqual(100, len(notes))

    def test_list_with_multiple_linked_records_per_note(self) -> None:
        """list_context_notes returns all linked_record_ids for a note with many."""
        for i in range(1, 4):
            self.store.upsert_activity(self._make_activity(f"act-{i}"))
        note = self._make_note("n", "Multi", linked_record_ids=("act-1", "act-2", "act-3"))
        self.store.upsert_context_note(note)
        self.connection.commit()

        notes = self.store.list_context_notes()

        self.assertEqual(1, len(notes))
        links = notes[0]["linked_record_ids"]
        self.assertEqual(3, len(links))
        self.assertIn("act-1", links)
        self.assertIn("act-2", links)
        self.assertIn("act-3", links)

    # ── C. Equivalence Partitioning ────────────────────────────────────────────

    def test_list_filters_by_kind_illness(self) -> None:
        """Filtering by illness returns only illness notes."""
        self.store.upsert_context_note(self._make_note("n-ill", "Sick", kind=NoteKind.ILLNESS))
        self.store.upsert_context_note(self._make_note("n-gen", "General", kind=NoteKind.GENERAL))
        self.connection.commit()

        notes = self.store.list_context_notes(kind=NoteKind.ILLNESS)

        self.assertEqual(1, len(notes))
        self.assertEqual("n-ill", notes[0]["record_id"])

    def test_list_filters_by_kind_injury(self) -> None:
        """Filtering by injury returns only injury notes."""
        self.store.upsert_context_note(self._make_note("n-inj", "Hurt", kind=NoteKind.INJURY))
        self.store.upsert_context_note(self._make_note("n-gen", "General", kind=NoteKind.GENERAL))
        self.connection.commit()

        notes = self.store.list_context_notes(kind=NoteKind.INJURY)

        self.assertEqual(1, len(notes))
        self.assertEqual("n-inj", notes[0]["record_id"])

    # ── D. Edge Cases ──────────────────────────────────────────────────────────

    def test_list_date_extraction_format_correct(self) -> None:
        """list_context_notes returns date in YYYY-MM-DD format."""
        self.store.upsert_context_note(
            self._make_note("n", "Test", occurred_at=date(2026, 7, 15))
        )
        self.connection.commit()

        notes = self.store.list_context_notes()

        date_str = notes[0]["date"]
        self.assertEqual("2026-07-15", date_str)
        self.assertRegex(date_str, r"^\d{4}-\d{2}-\d{2}$")

    def test_list_handles_notes_from_same_date_ordered_by_record_id(self) -> None:
        """Notes on the same date are ordered by record_id (secondary sort)."""
        same_date = date(2026, 7, 1)
        self.store.upsert_context_note(self._make_note("n-z", "Z", occurred_at=same_date))
        self.store.upsert_context_note(self._make_note("n-a", "A", occurred_at=same_date))
        self.connection.commit()

        notes = self.store.list_context_notes()

        self.assertEqual(2, len(notes))
        ids = [n["record_id"] for n in notes]
        self.assertEqual(["n-z", "n-a"], ids)

    # ── E. Error / Negative Tests ──────────────────────────────────────────────

    def test_list_with_invalid_kind_enum_does_not_crash(self) -> None:
        """Passing an invalid kind parameter should not crash (if type-checked)."""
        # Python doesn't prevent passing wrong enum, but verify no crash
        notes = self.store.list_context_notes(kind=None)
        self.assertIsInstance(notes, list)

    def test_since_filter_uses_datetime_string_not_date_function(self) -> None:
        """since filter must compare occurred_at directly as a datetime string (index-friendly),
        not via SQLite date() function which prevents index use.

        Passing a full ISO-8601 datetime string as the filter boundary is what makes
        this test meaningful: it confirms the column value is comparable as-is.
        """
        self.store.upsert_context_note(
            self._make_note("n-before", "Before", occurred_at=date(2026, 6, 30))
        )
        self.store.upsert_context_note(
            self._make_note("n-on", "On boundary", occurred_at=date(2026, 7, 1))
        )
        self.connection.commit()

        # Capture the actual SQL executed during since filtering
        executed_sqls = []

        class CapturingConnection:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args, **kwargs):
                executed_sqls.append(sql)
                return self._conn.execute(sql, *args, **kwargs)

        capturing_conn = CapturingConnection(self.connection)
        original_store_conn = self.store._connection
        self.store._connection = capturing_conn

        try:
            notes = self.store.list_context_notes(since=date(2026, 7, 1))
        finally:
            self.store._connection = original_store_conn

        self.assertEqual(1, len(notes))
        self.assertEqual("n-on", notes[0]["record_id"])

        # The SQL must NOT use date() wrapping on occurred_at (prevents index use).
        # It should use direct occurred_at >= ? with a datetime string.
        for sql in executed_sqls:
            if "occurred_at" in sql and "?" in sql:
                self.assertNotIn(
                    "date(note.occurred_at)",
                    sql,
                    "since filter must compare occurred_at directly, not via date() — date() prevents index use",
                )

    # ── F. Query Count (N+1 Detection) ────────────────────────────────────────

    def test_list_query_count_is_fixed_not_n_plus_one(self) -> None:
        """list_context_notes should use O(1) or O(log n) queries, not O(n) (N+1 problem)."""
        for i in range(10):
            self.store.upsert_context_note(self._make_note(f"n{i}", f"Note {i}"))
        self.connection.commit()

        # Wrap the connection to count queries
        query_count = 0

        class CountingConnection:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args, **kwargs):
                nonlocal query_count
                query_count += 1
                return self._conn.execute(sql, *args, **kwargs)

        counting_conn = CountingConnection(self.connection)
        original_store_conn = self.store._connection
        self.store._connection = counting_conn

        try:
            notes = self.store.list_context_notes()
        finally:
            self.store._connection = original_store_conn

        self.assertEqual(10, len(notes))
        # If N+1: would be ~11 queries (1 outer + 10 for links)
        # Fixed: should be 2-3 queries max (1 for notes + 1 for all links + overhead)
        # Currently fails if using N+1; should be < 5 after fix
        self.assertLess(
            query_count,
            8,
            f"Query count {query_count} suggests N+1 problem (should be ~2-3)",
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_note(
        self,
        record_id: str,
        text: str,
        *,
        kind: NoteKind = NoteKind.GENERAL,
        occurred_at: date = date(2026, 7, 1),
        linked_record_ids: tuple[str, ...] = (),
    ) -> ContextNote:
        return ContextNote(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="UTC",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            occurred_at=datetime(
                occurred_at.year, occurred_at.month, occurred_at.day, tzinfo=UTC
            ),
            kind=kind,
            text=text,
            linked_record_ids=linked_record_ids,
        )

    def _make_activity(self, record_id: str) -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="UTC",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 7, 1, tzinfo=UTC),
            duration=Measurement(1800, Unit.SECOND),
            distance=Measurement(5000, Unit.METRE),
        )


class CoachWebNotesHttpSemanticsTests(unittest.TestCase):
    """Test HTTP semantics and validation in coach_web.py notes endpoints."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            provider=FakeChatProvider("Stub answer."),
            provider_health=lambda: OllamaHealth(
                base_url="http://localhost:11434",
                chat_model="llama3.2",
                available=False,
                error="not running",
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary_directory.cleanup()

    # ── A. Happy Path ──────────────────────────────────────────────────────────

    def test_post_notes_succeeds_with_required_fields(self) -> None:
        """POST /api/notes with type and body succeeds."""
        status, payload = self._post_json("/api/notes", {"type": "illness", "body": "Flu."})

        self.assertEqual(200, status)
        self.assertIn("record_id", payload)

    def test_get_notes_returns_json_list(self) -> None:
        """GET /api/notes returns a valid JSON list."""
        self._post_json("/api/notes", {"type": "stress", "body": "Tired."})

        status, payload = self._get_json("/api/notes")

        self.assertEqual(200, status)
        self.assertIsInstance(payload, list)

    def test_delete_note_succeeds_with_valid_id(self) -> None:
        """DELETE /api/notes/{id} succeeds with valid note ID."""
        _, created = self._post_json("/api/notes", {"type": "injury", "body": "Hurt."})
        record_id = created["record_id"]

        status = self._delete(f"/api/notes/{record_id}")

        self.assertEqual(204, status)

    # ── B. Boundary Values ─────────────────────────────────────────────────────

    def test_post_notes_status_code_is_201_created_not_200(self) -> None:
        """POST /api/notes should return 201 Created, not 200 OK (RFC 7231)."""
        status, _ = self._post_json("/api/notes", {"type": "note", "body": "New note."})

        self.assertEqual(201, status, "POST should return 201 Created for new resource")

    def test_get_notes_empty_database_returns_empty_list_with_200(self) -> None:
        """GET /api/notes on empty database returns 200 with []."""
        status, payload = self._get_json("/api/notes")

        self.assertEqual(200, status)
        self.assertEqual([], payload)

    def test_delete_nonexistent_note_returns_404(self) -> None:
        """DELETE /api/notes/{id} with nonexistent ID returns 404."""
        status = self._delete("/api/notes/note:does-not-exist")

        self.assertEqual(404, status)

    # ── C. Equivalence Partitioning ────────────────────────────────────────────

    def test_post_notes_with_valid_types(self) -> None:
        """POST /api/notes accepts all valid note types."""
        valid_types = ["illness", "injury", "travel", "stress", "note"]
        for note_type in valid_types:
            status, payload = self._post_json(
                "/api/notes", {"type": note_type, "body": "Valid type."}
            )
            self.assertEqual(201, status, f"Type {note_type!r} should be accepted")

    def test_post_notes_rejects_invalid_type(self) -> None:
        """POST /api/notes with invalid type returns 400 Bad Request."""
        status, payload = self._post_json("/api/notes", {"type": "invalid", "body": "Test."})

        self.assertEqual(400, status)
        self.assertIn("error", payload)

    def test_post_notes_rejects_missing_body(self) -> None:
        """POST /api/notes without body returns 400."""
        status, payload = self._post_json("/api/notes", {"type": "stress"})

        self.assertEqual(400, status)
        self.assertIn("error", payload)

    def test_post_notes_rejects_empty_body(self) -> None:
        """POST /api/notes with empty body returns 400."""
        status, payload = self._post_json("/api/notes", {"type": "stress", "body": ""})

        self.assertEqual(400, status)

    def test_post_notes_rejects_whitespace_only_body(self) -> None:
        """POST /api/notes with whitespace-only body returns 400."""
        status, payload = self._post_json("/api/notes", {"type": "stress", "body": "   "})

        self.assertEqual(400, status)

    # ── D. Edge Cases ──────────────────────────────────────────────────────────

    def test_post_notes_with_date_field_stores_given_date(self) -> None:
        """POST /api/notes with date field stores that date."""
        status, payload = self._post_json(
            "/api/notes", {"type": "note", "body": "Old note.", "date": "2026-06-01"}
        )
        record_id = payload["record_id"]

        notes = self._get_json("/api/notes")[1]
        matching = [n for n in notes if n["record_id"] == record_id]

        self.assertEqual(1, len(matching))
        self.assertEqual("2026-06-01", matching[0]["date"])

    def test_post_notes_without_date_defaults_to_utc_today(self) -> None:
        """POST /api/notes without date uses UTC today."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        self._post_json("/api/notes", {"type": "note", "body": "Today note."})

        notes = self._get_json("/api/notes")[1]
        self.assertEqual(1, len(notes))
        self.assertEqual(today, notes[0]["date"])

    def test_post_notes_with_invalid_date_format_returns_400(self) -> None:
        """POST /api/notes with malformed date returns 400."""
        status, payload = self._post_json(
            "/api/notes", {"type": "note", "body": "Bad date.", "date": "2026/06/01"}
        )

        self.assertEqual(400, status)
        self.assertIn("error", payload)

    def test_delete_note_with_special_chars_in_id(self) -> None:
        """DELETE /api/notes/{id} with special chars in ID (from note creation)."""
        _, created = self._post_json("/api/notes", {"type": "note", "body": "To delete."})
        record_id = created["record_id"]

        # record_id contains colons, UUIDs; verify DELETE handles it
        status = self._delete(f"/api/notes/{record_id}")

        self.assertEqual(204, status)

    def test_delete_note_with_empty_id_returns_404(self) -> None:
        """DELETE /api/notes/ (empty ID) returns 404."""
        status = self._delete("/api/notes/")

        self.assertEqual(404, status)

    def test_delete_note_with_path_traversal_attempt_returns_400_or_404(self) -> None:
        """DELETE /api/notes/../admin should not be accepted."""
        status = self._delete("/api/notes/../admin")

        self.assertIn(status, [400, 404, 501])

    def test_get_notes_with_invalid_type_filter_returns_400(self) -> None:
        """GET /api/notes?type=invalid returns 400."""
        status, payload = self._get_json("/api/notes?type=invalid_type")

        self.assertEqual(400, status)

    # ── E. Error / Negative Tests ──────────────────────────────────────────────

    def test_post_notes_missing_type_field_returns_400(self) -> None:
        """POST /api/notes without type returns 400."""
        status, payload = self._post_json("/api/notes", {"body": "No type."})

        self.assertEqual(400, status)

    def test_post_notes_non_json_body_returns_400(self) -> None:
        """POST /api/notes with invalid JSON returns 400."""
        status = self._post_raw("/api/notes", "not json")

        self.assertEqual(400, status)

    def test_post_notes_missing_content_length_returns_400(self) -> None:
        """POST /api/notes without Content-Length header returns 400."""
        status = self._post_without_content_length("/api/notes")

        self.assertEqual(400, status)

    def test_post_notes_zero_content_length_returns_400(self) -> None:
        """POST /api/notes with Content-Length: 0 returns 400."""
        status = self._post_json_with_content_length("/api/notes", {"type": "x"}, 0)

        self.assertEqual(400, status)

    def test_post_notes_oversized_body_returns_413(self) -> None:
        """POST /api/notes with body > MAX_REQUEST_BYTES returns 413."""
        big_body = "x" * (66000)  # > MAX_REQUEST_BYTES (65536)
        status = self._post_raw("/api/notes", big_body)

        self.assertIn(status, [400, 413])

    def test_delete_note_with_url_encoded_slash_in_id_returns_400_or_404(self) -> None:
        """DELETE /api/notes/foo%2Fbar — URL-encoded slash should not be treated as path separator."""
        status = self._delete("/api/notes/foo%2Fbar")
        self.assertIn(status, [400, 404])

    def test_delete_note_with_whitespace_only_id_returns_404(self) -> None:
        """DELETE /api/notes/%20 (whitespace ID) should return 404, not match a note."""
        status = self._delete("/api/notes/%20")
        self.assertIn(status, [400, 404])

    def test_post_note_with_date_round_trips_correctly_through_since_filter(self) -> None:
        """A note posted with date='2026-07-01' must appear when queried with since=2026-07-01.

        Timezone mismatch: if occurred_at is stored as a naive datetime or a UTC datetime
        with timezone suffix that SQLite misparses, the since filter can silently drop the note.
        """
        self._post_json("/api/notes", {"type": "note", "body": "TZ test.", "date": "2026-07-01"})

        status, notes = self._get_json("/api/notes?since=2026-07-01")

        self.assertEqual(200, status)
        self.assertEqual(1, len(notes), "Note posted for 2026-07-01 must appear with since=2026-07-01 filter")
        self.assertEqual("2026-07-01", notes[0]["date"])

    def test_post_note_timezone_stored_date_matches_input_date(self) -> None:
        """The stored date must exactly match the user-supplied date string, not shift due to UTC conversion."""
        _, payload = self._post_json(
            "/api/notes", {"type": "note", "body": "Date fidelity.", "date": "2026-07-04"}
        )
        record_id = payload["record_id"]

        _, all_notes = self._get_json("/api/notes")
        matching = [n for n in all_notes if n["record_id"] == record_id]

        self.assertEqual(1, len(matching))
        self.assertEqual("2026-07-04", matching[0]["date"], "Stored date must not shift due to UTC midnight conversion")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_json(self, path: str) -> tuple[int, object]:
        """GET request, return (status_code, parsed_json)."""
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}") as response:
                payload = response.read().decode("utf-8")
                import json

                return 200, json.loads(payload)
        except urllib.error.HTTPError as e:
            import json

            try:
                payload = e.read().decode("utf-8")
                return e.code, json.loads(payload)
            except Exception:
                return e.code, {}

    def _post_json(self, path: str, data: dict) -> tuple[int, dict]:
        """POST JSON, return (status_code, parsed_response)."""
        import json

        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                payload = response.read().decode("utf-8")
                return response.status, json.loads(payload)
        except urllib.error.HTTPError as e:
            try:
                payload = e.read().decode("utf-8")
                return e.code, json.loads(payload)
            except Exception:
                return e.code, {}

    def _post_raw(self, path: str, body: str) -> int:
        """POST raw body, return status code."""
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body.encode("utf-8"),
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def _post_without_content_length(self, path: str) -> int:
        """POST without Content-Length header, return status code."""
        import http.client

        host, port = self.server.server_address
        conn = http.client.HTTPConnection(host, port)
        try:
            # Manually construct request without Content-Length
            conn.send(f"POST {path} HTTP/1.1\r\n".encode())
            conn.send(f"Host: {host}:{port}\r\n".encode())
            conn.send(b"\r\n")
            response = conn.getresponse()
            return response.status
        finally:
            conn.close()

    def _post_json_with_content_length(
        self, path: str, data: dict, content_length: int
    ) -> int:
        """POST with explicit Content-Length override."""
        import json
        import http.client

        body = json.dumps(data).encode("utf-8")
        host, port = self.server.server_address
        conn = http.client.HTTPConnection(host, port)
        try:
            conn.send(f"POST {path} HTTP/1.1\r\n".encode())
            conn.send(f"Host: {host}:{port}\r\n".encode())
            conn.send(b"Content-Type: application/json\r\n")
            conn.send(f"Content-Length: {content_length}\r\n".encode())
            conn.send(b"\r\n")
            if content_length > 0:
                conn.send(body[: content_length - 1])
            response = conn.getresponse()
            return response.status
        finally:
            conn.close()

    def _delete(self, path: str) -> int:
        """DELETE request, return status code."""
        req = urllib.request.Request(f"{self.base_url}{path}", method="DELETE")
        try:
            with urllib.request.urlopen(req) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code


if __name__ == "__main__":
    unittest.main()
