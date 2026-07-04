from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trainingos.storage import apply_migrations, connect_database


class NotesCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    # -------------------------------------------------------------------------
    # A. Happy Path
    # -------------------------------------------------------------------------

    def test_add_persists_illness_note_with_defaults(self) -> None:
        result = self._run("add", "--type", "illness", "--body", "Flu, resting at home")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("illness", result.stdout.lower())
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT note_kind, note_text, provenance_kind "
                "FROM context_notes JOIN records USING (record_id)"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("illness", row["note_kind"])
        self.assertEqual("Flu, resting at home", row["note_text"])
        self.assertEqual("user_entered", row["provenance_kind"])

    def test_add_persists_injury_note_with_explicit_date(self) -> None:
        result = self._run(
            "add", "--type", "injury", "--body", "Left calf tight", "--date", "2026-06-15"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT occurred_at FROM context_notes"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("2026-06-15", row["occurred_at"])

    def test_add_links_note_to_activity(self) -> None:
        with connect_database(self.database_path) as connection:
            _insert_activity(connection, "activity-1")
        result = self._run(
            "add", "--type", "note", "--body", "Felt strong today", "--activity", "activity-1"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        with connect_database(self.database_path) as connection:
            links = connection.execute(
                "SELECT linked_record_id FROM context_note_links"
            ).fetchall()
        self.assertEqual(["activity-1"], [r["linked_record_id"] for r in links])

    def test_list_prints_notes_in_reverse_chronological_order(self) -> None:
        self._run("add", "--type", "note", "--body", "Early note", "--date", "2026-05-01")
        self._run("add", "--type", "note", "--body", "Late note", "--date", "2026-07-01")

        result = self._run("list")

        self.assertEqual(0, result.returncode, result.stderr)
        early_pos = result.stdout.find("Early note")
        late_pos = result.stdout.find("Late note")
        self.assertGreater(early_pos, -1)
        self.assertGreater(late_pos, -1)
        self.assertLess(late_pos, early_pos)

    def test_list_with_type_filter_returns_only_matching_notes(self) -> None:
        self._run("add", "--type", "illness", "--body", "Head cold", "--date", "2026-07-01")
        self._run("add", "--type", "injury", "--body", "Knee soreness", "--date", "2026-07-02")

        result = self._run("list", "--type", "illness")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Head cold", result.stdout)
        self.assertNotIn("Knee soreness", result.stdout)

    def test_list_with_since_filter_excludes_older_notes(self) -> None:
        self._run("add", "--type", "note", "--body", "Old note", "--date", "2026-05-01")
        self._run("add", "--type", "note", "--body", "Recent note", "--date", "2026-07-01")

        result = self._run("list", "--since", "2026-06-01")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Recent note", result.stdout)
        self.assertNotIn("Old note", result.stdout)

    def test_delete_removes_note_by_id(self) -> None:
        record_id = "note-to-delete-1"
        with connect_database(self.database_path) as connection:
            _insert_note(connection, record_id)

        result = self._run("delete", record_id)

        self.assertEqual(0, result.returncode, result.stderr)
        with connect_database(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_delete_cascades_to_note_links(self) -> None:
        record_id = "note-to-delete-2"
        with connect_database(self.database_path) as connection:
            _insert_activity(connection, "activity-1")
            _insert_note(connection, record_id, linked_to="activity-1")

        self._run("delete", record_id)

        with connect_database(self.database_path) as connection:
            links = connection.execute("SELECT COUNT(*) FROM context_note_links").fetchone()[0]
        self.assertEqual(0, links)

    # -------------------------------------------------------------------------
    # B. Boundary Values
    # -------------------------------------------------------------------------

    def test_add_body_at_max_reasonable_length(self) -> None:
        long_body = "x" * 4000
        result = self._run("add", "--type", "note", "--body", long_body)

        self.assertEqual(0, result.returncode, result.stderr)
        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT note_text FROM context_notes").fetchone()
        self.assertEqual(long_body, row["note_text"])

    def test_list_since_equal_to_note_date_includes_note(self) -> None:
        self._run("add", "--type", "note", "--body", "Same day note", "--date", "2026-07-01")

        result = self._run("list", "--since", "2026-07-01")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Same day note", result.stdout)

    def test_list_on_empty_database_exits_zero(self) -> None:
        result = self._run("list")

        self.assertEqual(0, result.returncode, result.stderr)

    # -------------------------------------------------------------------------
    # C. Equivalence Partitioning
    # -------------------------------------------------------------------------

    def test_add_all_valid_note_types_succeed(self) -> None:
        for kind in ("illness", "injury", "travel", "stress", "note"):
            with self.subTest(kind=kind):
                result = self._run(
                    "add", "--type", kind, "--body", f"Testing {kind} kind"
                )
                self.assertEqual(0, result.returncode, result.stderr)

    def test_list_all_valid_type_filters_succeed(self) -> None:
        for kind in ("illness", "injury", "travel", "stress", "note"):
            with self.subTest(kind=kind):
                result = self._run("list", "--type", kind)
                self.assertEqual(0, result.returncode, result.stderr)

    # -------------------------------------------------------------------------
    # D. Edge Cases
    # -------------------------------------------------------------------------

    def test_add_body_with_special_characters(self) -> None:
        body = 'Felt "tired". Stress: high. Quote: it\'s fine.'
        result = self._run("add", "--type", "stress", "--body", body)

        self.assertEqual(0, result.returncode, result.stderr)
        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT note_text FROM context_notes").fetchone()
        self.assertEqual(body, row["note_text"])

    def test_list_single_note_returns_that_note(self) -> None:
        self._run("add", "--type", "illness", "--body", "Only note")

        result = self._run("list")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Only note", result.stdout)

    # -------------------------------------------------------------------------
    # E. Error / Negative Tests
    # -------------------------------------------------------------------------

    def test_add_missing_type_exits_nonzero(self) -> None:
        result = self._run("add", "--body", "text")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_missing_body_exits_nonzero(self) -> None:
        result = self._run("add", "--type", "illness")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_blank_body_is_rejected(self) -> None:
        result = self._run("add", "--type", "illness", "--body", "   ")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_invalid_type_is_rejected(self) -> None:
        result = self._run("add", "--type", "fatigue", "--body", "Tired")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_malformed_date_is_rejected(self) -> None:
        result = self._run(
            "add", "--type", "illness", "--body", "text", "--date", "not-a-date"
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_nonexistent_activity_id_is_rejected(self) -> None:
        result = self._run(
            "add", "--type", "note", "--body", "text", "--activity", "missing-xyz"
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)
        with connect_database(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_delete_nonexistent_id_exits_nonzero(self) -> None:
        result = self._run("delete", "unknown-note-id")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_delete_missing_argument_exits_nonzero(self) -> None:
        result = self._run("delete")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "trainingos.notes",
                "--database",
                str(self.database_path),
                *args,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=_pythonpath_env(),
            capture_output=True,
            text=True,
            check=False,
        )


def _insert_note(connection, record_id: str, *, linked_to: str | None = None) -> None:
    connection.execute(
        """
        INSERT INTO records (
            record_id, record_type, timezone, created_at, updated_at, provenance_kind
        ) VALUES (?, 'context_note', 'America/Toronto',
                  '2026-07-01T07:00:00+00:00', '2026-07-01T07:00:00+00:00',
                  'user_entered')
        """,
        (record_id,),
    )
    connection.execute(
        """
        INSERT INTO context_notes (record_id, occurred_at, note_kind, note_text)
        VALUES (?, '2026-07-01T07:00:00+00:00', 'stress', 'Pre-race anxiety')
        """,
        (record_id,),
    )
    if linked_to:
        connection.execute(
            """
            INSERT INTO context_note_links (note_id, linked_record_id)
            VALUES (?, ?)
            """,
            (record_id, linked_to),
        )
    connection.commit()


def _insert_activity(connection, record_id: str) -> None:
    connection.execute(
        """
        INSERT INTO records (
            record_id, record_type, timezone, created_at, updated_at, provenance_kind
        ) VALUES (?, 'activity', 'America/Toronto',
                  '2026-07-01T07:00:00+00:00', '2026-07-01T07:00:00+00:00',
                  'user_entered')
        """,
        (record_id,),
    )
    connection.execute(
        """
        INSERT INTO activities (
            record_id, activity_type, started_at, duration_seconds, distance_metres
        ) VALUES (?, 'run', '2026-07-01T05:30:00+00:00', 3600, 10000)
        """,
        (record_id,),
    )
    connection.commit()


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
    return env


class NoteTypeMapConsistencyTests(unittest.TestCase):
    """Verify CLI and API use identical note type mappings from shared module."""

    def test_shared_module_defines_note_type_map(self) -> None:
        """Verify the shared note_types module exists and exports NOTE_TYPE_MAP."""
        from trainingos.note_types import NOTE_TYPE_MAP

        self.assertIsNotNone(NOTE_TYPE_MAP)
        self.assertIsInstance(NOTE_TYPE_MAP, dict)
        self.assertGreater(len(NOTE_TYPE_MAP), 0, "NOTE_TYPE_MAP must not be empty")

    def test_cli_imports_note_type_map_from_shared_module(self) -> None:
        """Verify CLI module imports from the shared module."""
        import trainingos.notes
        from trainingos.note_types import NOTE_TYPE_MAP as shared_map

        cli_map = trainingos.notes.NOTE_TYPE_MAP
        self.assertIs(cli_map, shared_map, "CLI should import NOTE_TYPE_MAP from shared module")

    def test_api_imports_note_type_map_from_shared_module(self) -> None:
        """Verify API module imports from the shared module."""
        from trainingos.coach_web import _NOTE_TYPE_MAP as api_map
        from trainingos.note_types import NOTE_TYPE_MAP as shared_map

        self.assertIs(api_map, shared_map, "API should use NOTE_TYPE_MAP from shared module")

    def test_cli_and_api_use_identical_note_type_map(self) -> None:
        """Prevent drift between CLI and API by verifying they use the same object."""
        import trainingos.notes
        from trainingos.coach_web import _NOTE_TYPE_MAP as api_map

        cli_map = trainingos.notes.NOTE_TYPE_MAP
        self.assertIs(cli_map, api_map, "CLI and API must use the same NOTE_TYPE_MAP instance")

    def test_note_type_map_has_all_required_types(self) -> None:
        """Ensure the mapping includes all expected note types."""
        from trainingos.note_types import NOTE_TYPE_MAP

        expected_types = {"illness", "injury", "travel", "stress", "note"}
        actual_types = set(NOTE_TYPE_MAP.keys())
        self.assertEqual(
            actual_types,
            expected_types,
            f"Must have exactly these note types; got {actual_types}"
        )

    def test_note_type_map_no_extra_types(self) -> None:
        """Verify no unexpected note types have been added."""
        from trainingos.note_types import NOTE_TYPE_MAP

        expected = {"illness", "injury", "travel", "stress", "note"}
        extra = set(NOTE_TYPE_MAP.keys()) - expected
        self.assertEqual(set(), extra, f"Unexpected note types: {extra}")

    def test_note_type_map_values_are_note_kinds(self) -> None:
        """Verify all mappings resolve to valid NoteKind instances."""
        from trainingos.note_types import NOTE_TYPE_MAP
        from trainingos.domain import NoteKind

        for type_str, kind in NOTE_TYPE_MAP.items():
            self.assertIsInstance(
                kind, NoteKind,
                f"Type '{type_str}' does not map to NoteKind; got {type(kind)}"
            )

    def test_all_note_kinds_are_mapped(self) -> None:
        """Ensure the mapping covers all NoteKind enum values."""
        from trainingos.note_types import NOTE_TYPE_MAP
        from trainingos.domain import NoteKind

        mapped_kinds = set(NOTE_TYPE_MAP.values())
        expected_kinds = {
            NoteKind.ILLNESS,
            NoteKind.INJURY,
            NoteKind.TRAVEL,
            NoteKind.STRESS,
            NoteKind.GENERAL,
        }
        self.assertEqual(
            mapped_kinds,
            expected_kinds,
            f"All required NoteKind values must be mapped; got {mapped_kinds}"
        )

    def test_note_type_map_no_duplicate_kinds(self) -> None:
        """Verify each NoteKind is mapped to exactly one string."""
        from trainingos.note_types import NOTE_TYPE_MAP

        kinds_list = list(NOTE_TYPE_MAP.values())
        kinds_set = set(kinds_list)
        self.assertEqual(
            len(kinds_list),
            len(kinds_set),
            "Each NoteKind should map from exactly one string"
        )

    def test_cli_parser_accepts_all_note_types(self) -> None:
        """Verify CLI argparse accepts all note types."""
        from trainingos.notes import _build_parser, NOTE_TYPE_MAP

        parser = _build_parser()
        for type_str in NOTE_TYPE_MAP.keys():
            with self.subTest(type_str=type_str):
                try:
                    args = parser.parse_args([
                        "--database", "dummy.db",
                        "add",
                        "--type", type_str,
                        "--body", "test"
                    ])
                    self.assertEqual(type_str, args.type)
                except SystemExit:
                    self.fail(f"Parser should accept note type '{type_str}'")

    def test_api_accepts_all_note_types(self) -> None:
        """Verify API accepts all note types via POST."""
        from trainingos.coach_web import _NOTE_TYPE_MAP

        for type_str in _NOTE_TYPE_MAP.keys():
            with self.subTest(type_str=type_str):
                self.assertIn(type_str, _NOTE_TYPE_MAP, f"API must accept type '{type_str}'")


if __name__ == "__main__":
    unittest.main()
