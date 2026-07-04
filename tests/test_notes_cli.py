"""Tests for context note CLI storage layer (trainingos.notes)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.domain import (
    Activity,
    ActivityType,
    Measurement,
    NoteKind,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.notes import NoteRow, add_note, delete_note, list_notes
from trainingos.storage import apply_migrations, connect_database

_TIMEZONE = "America/Toronto"
_TODAY = date(2026, 7, 3)
_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def _metadata_args(record_id: str) -> dict:
    return {
        "timezone": _TIMEZONE,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


class NotesCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.connection = connect_database(
            Path(self.tmpdir.name) / "training.sqlite3"
        )
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmpdir.cleanup()

    # ---- A. Happy Path -------------------------------------------------------

    def test_add_persists_note_with_all_required_fields(self) -> None:
        record_id = add_note(
            self.connection,
            kind="illness",
            body="Sore throat, skipping tempo",
            date=date(2026, 7, 1),
            now=_NOW,
        )

        row = self.connection.execute(
            """
            SELECT cn.note_kind, cn.note_text, r.provenance_kind
            FROM context_notes cn
            JOIN records r USING (record_id)
            WHERE cn.record_id = ?
            """,
            (record_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("illness", row["note_kind"])
        self.assertEqual("Sore throat, skipping tempo", row["note_text"])
        self.assertEqual("user_entered", row["provenance_kind"])

    def test_add_with_activity_link_creates_link_row(self) -> None:
        self._insert_activity("act-1")
        record_id = add_note(
            self.connection,
            kind="injury",
            body="Left calf tightness post-run",
            date=date(2026, 7, 1),
            activity_id="act-1",
            now=_NOW,
        )

        link = self.connection.execute(
            "SELECT linked_record_id FROM context_note_links WHERE note_id = ?",
            (record_id,),
        ).fetchone()
        self.assertIsNotNone(link)
        self.assertEqual("act-1", link["linked_record_id"])

    def test_list_returns_notes_in_reverse_chronological_order(self) -> None:
        for day, kind in [
            (date(2026, 6, 1), "stress"),
            (date(2026, 6, 15), "illness"),
            (date(2026, 7, 1), "note"),
        ]:
            add_note(self.connection, kind=kind, body=f"Note on {day}", date=day, now=_NOW)

        rows = list_notes(self.connection)

        self.assertEqual(3, len(rows))
        self.assertEqual(date(2026, 7, 1), rows[0].occurred_at.date())
        self.assertEqual(date(2026, 6, 15), rows[1].occurred_at.date())
        self.assertEqual(date(2026, 6, 1), rows[2].occurred_at.date())

    def test_list_with_type_filter_returns_only_matching_kind(self) -> None:
        add_note(self.connection, kind="stress", body="High stress week", date=date(2026, 7, 1), now=_NOW)
        add_note(self.connection, kind="injury", body="Shin splint flare", date=date(2026, 7, 2), now=_NOW)

        rows = list_notes(self.connection, kind="injury")

        self.assertEqual(1, len(rows))
        self.assertEqual("injury", rows[0].kind)

    def test_list_with_since_filter_excludes_older_notes(self) -> None:
        add_note(self.connection, kind="note", body="Old note", date=date(2026, 5, 1), now=_NOW)
        add_note(self.connection, kind="note", body="Recent note", date=date(2026, 7, 1), now=_NOW)

        rows = list_notes(self.connection, since=date(2026, 6, 1))

        self.assertEqual(1, len(rows))
        self.assertEqual("Recent note", rows[0].body)

    def test_delete_removes_note_and_links_by_record_id(self) -> None:
        self._insert_activity("act-2")
        record_id = add_note(
            self.connection,
            kind="travel",
            body="Business trip, no training",
            date=date(2026, 7, 1),
            activity_id="act-2",
            now=_NOW,
        )

        delete_note(self.connection, record_id)

        note_row = self.connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = ?", (record_id,)
        ).fetchone()
        record_row = self.connection.execute(
            "SELECT 1 FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        link_row = self.connection.execute(
            "SELECT 1 FROM context_note_links WHERE note_id = ?", (record_id,)
        ).fetchone()
        self.assertIsNone(note_row)
        self.assertIsNone(record_row)
        self.assertIsNone(link_row)

    # ---- B. Boundary Value Analysis ------------------------------------------

    def test_add_without_date_defaults_to_today(self) -> None:
        record_id = add_note(
            self.connection,
            kind="note",
            body="Rest day",
            now=_NOW,
        )

        row = self.connection.execute(
            "SELECT occurred_at FROM context_notes WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        occurred_date = datetime.fromisoformat(row["occurred_at"]).date()
        self.assertEqual(_TODAY, occurred_date)

    def test_list_since_boundary_includes_same_day_notes(self) -> None:
        add_note(self.connection, kind="note", body="Boundary note", date=date(2026, 6, 1), now=_NOW)

        rows = list_notes(self.connection, since=date(2026, 6, 1))

        self.assertEqual(1, len(rows))

    def test_list_since_boundary_excludes_day_before(self) -> None:
        add_note(self.connection, kind="note", body="Day before note", date=date(2026, 5, 31), now=_NOW)

        rows = list_notes(self.connection, since=date(2026, 6, 1))

        self.assertEqual(0, len(rows))

    def test_note_body_at_max_reasonable_length_roundtrips(self) -> None:
        long_body = "x" * 4000
        record_id = add_note(
            self.connection,
            kind="note",
            body=long_body,
            date=date(2026, 7, 1),
            now=_NOW,
        )

        rows = list_notes(self.connection)

        self.assertEqual(long_body, rows[0].body)

    # ---- C. Equivalence Partitioning -----------------------------------------

    def test_add_all_valid_cli_note_kinds(self) -> None:
        cli_kinds = ["illness", "injury", "travel", "stress", "note"]
        for i, kind in enumerate(cli_kinds):
            add_note(
                self.connection,
                kind=kind,
                body=f"Test note {kind}",
                date=date(2026, 7, i + 1),
                now=_NOW,
            )

        rows = list_notes(self.connection)
        persisted_kinds = {r.kind for r in rows}
        self.assertEqual(set(cli_kinds), persisted_kinds)

    def test_list_combined_type_and_since_filters(self) -> None:
        add_note(self.connection, kind="stress", body="Old stress", date=date(2026, 6, 1), now=_NOW)
        add_note(self.connection, kind="stress", body="Recent stress", date=date(2026, 6, 20), now=_NOW)
        add_note(self.connection, kind="injury", body="Recent injury", date=date(2026, 6, 20), now=_NOW)

        rows = list_notes(self.connection, kind="stress", since=date(2026, 6, 15))

        self.assertEqual(1, len(rows))
        self.assertEqual("Recent stress", rows[0].body)

    def test_delete_nonexistent_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            delete_note(self.connection, "does-not-exist")

    # ---- D. Edge Cases -------------------------------------------------------

    def test_list_with_no_notes_returns_empty(self) -> None:
        rows = list_notes(self.connection)

        self.assertEqual([], rows)

    def test_add_note_without_activity_link_leaves_link_table_empty(self) -> None:
        record_id = add_note(
            self.connection,
            kind="note",
            body="Standalone note",
            date=date(2026, 7, 1),
            now=_NOW,
        )

        links = self.connection.execute(
            "SELECT 1 FROM context_note_links WHERE note_id = ?", (record_id,)
        ).fetchall()
        self.assertEqual([], links)

    def test_add_is_idempotent_on_same_record_id(self) -> None:
        record_id = add_note(
            self.connection,
            kind="note",
            body="Original body",
            date=date(2026, 7, 1),
            now=_NOW,
        )
        add_note(
            self.connection,
            kind="note",
            body="Updated body",
            date=date(2026, 7, 1),
            record_id=record_id,
            now=_NOW,
        )

        rows = list_notes(self.connection)
        self.assertEqual(1, len(rows))
        self.assertEqual("Updated body", rows[0].body)

    def test_list_with_filters_matching_nothing_returns_empty(self) -> None:
        add_note(self.connection, kind="stress", body="Stress note", date=date(2026, 6, 1), now=_NOW)

        rows = list_notes(self.connection, kind="injury", since=date(2026, 7, 1))

        self.assertEqual([], rows)

    # ---- E. Error / Negative Tests -------------------------------------------

    def test_add_with_blank_body_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "text"):
            add_note(self.connection, kind="note", body="   ", now=_NOW)

    def test_add_with_invalid_kind_raises_value_error(self) -> None:
        with self.assertRaises((ValueError, KeyError)):
            add_note(self.connection, kind="hangover", body="Not a real kind", now=_NOW)

    def test_add_with_invalid_date_format_raises_value_error(self) -> None:
        with self.assertRaises((ValueError, TypeError)):
            add_note(self.connection, kind="note", body="Some text", date="07/01/2026", now=_NOW)  # type: ignore[arg-type]

    def test_delete_with_blank_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            delete_note(self.connection, "   ")

    # ---- F. State / Integration Tests ----------------------------------------

    def test_add_note_illness_kind_appears_in_retrieval_document(self) -> None:
        from trainingos.retrieval import generate_retrieval_documents

        add_note(
            self.connection,
            kind="illness",
            body="Fighting a cold, reduced load",
            date=date(2026, 7, 1),
            now=_NOW,
        )
        generate_retrieval_documents(self.connection, now=_NOW)

        doc = self.connection.execute(
            "SELECT body FROM retrieval_documents WHERE document_type = 'note'"
        ).fetchone()
        self.assertIsNotNone(doc)
        self.assertIn("Fighting a cold", doc["body"])

    def test_add_note_injury_kind_appears_in_retrieval_document(self) -> None:
        from trainingos.retrieval import generate_retrieval_documents

        add_note(
            self.connection,
            kind="injury",
            body="Plantar fascia soreness",
            date=date(2026, 7, 1),
            now=_NOW,
        )
        generate_retrieval_documents(self.connection, now=_NOW)

        doc = self.connection.execute(
            "SELECT body FROM retrieval_documents WHERE document_type = 'note'"
        ).fetchone()
        self.assertIsNotNone(doc)
        self.assertIn("Plantar fascia soreness", doc["body"])

    def test_add_note_travel_kind_appears_in_retrieval_document(self) -> None:
        from trainingos.retrieval import generate_retrieval_documents

        add_note(
            self.connection,
            kind="travel",
            body="Conference in New York, no gym access",
            date=date(2026, 7, 1),
            now=_NOW,
        )
        generate_retrieval_documents(self.connection, now=_NOW)

        doc = self.connection.execute(
            "SELECT body FROM retrieval_documents WHERE document_type = 'note'"
        ).fetchone()
        self.assertIsNotNone(doc)
        self.assertIn("Conference in New York", doc["body"])

    def test_refresh_regenerates_document_after_note_is_added(self) -> None:
        from trainingos.retrieval import generate_retrieval_documents

        first = generate_retrieval_documents(self.connection, now=_NOW)
        add_note(
            self.connection,
            kind="stress",
            body="Work deadline, poor sleep",
            date=date(2026, 7, 1),
            now=_NOW,
        )
        second = generate_retrieval_documents(self.connection, now=_NOW)

        doc = self.connection.execute(
            "SELECT body FROM retrieval_documents WHERE document_type = 'note'"
        ).fetchone()
        self.assertIsNotNone(doc)
        self.assertIn("Work deadline", doc["body"])

    def test_refresh_updates_activity_document_after_linked_note_changes(self) -> None:
        from trainingos.retrieval import generate_retrieval_documents

        self._insert_activity("act-3")
        record_id = add_note(
            self.connection,
            kind="injury",
            body="Initial calf tightness note",
            date=date(2026, 7, 1),
            activity_id="act-3",
            now=_NOW,
        )
        generate_retrieval_documents(self.connection, now=_NOW)

        updated_now = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)
        add_note(
            self.connection,
            kind="injury",
            body="Calf tightness resolved after two rest days",
            date=date(2026, 7, 1),
            activity_id="act-3",
            record_id=record_id,
            now=updated_now,
        )
        generate_retrieval_documents(self.connection, now=updated_now)

        doc = self.connection.execute(
            """
            SELECT body FROM retrieval_documents
            WHERE document_type = 'activity' AND source_record_id = 'act-3'
            """
        ).fetchone()
        self.assertIsNotNone(doc)
        self.assertIn("resolved after two rest days", doc["body"])

    # ---- Helpers -------------------------------------------------------------

    def _insert_activity(self, record_id: str) -> None:
        self.store.upsert_activity(
            Activity(
                metadata=_record_metadata(record_id),
                activity_type=ActivityType.RUN,
                started_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
                duration=Measurement(3600, Unit.SECOND),
                distance=Measurement(10000, Unit.METRE),
                title="Morning easy run",
            )
        )


def _record_metadata(record_id: str):
    from trainingos.domain import Provenance, ProvenanceKind, RecordMetadata

    return RecordMetadata(
        record_id=record_id,
        timezone=_TIMEZONE,
        created_at=_NOW,
        updated_at=_NOW,
        provenance=Provenance(ProvenanceKind.USER_ENTERED),
    )


if __name__ == "__main__":
    unittest.main()
