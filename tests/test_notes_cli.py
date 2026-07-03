from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.config import DATABASE_PATH_ENV
from trainingos.storage import apply_migrations, connect_database


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
    return env


def _run_notes(args: list[str], database_path: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    resolved_env = env or _pythonpath_env()
    return subprocess.run(
        [sys.executable, "-m", "trainingos.notes", *args],
        cwd=Path(__file__).resolve().parents[1],
        env={**resolved_env, DATABASE_PATH_ENV: str(database_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def _note_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]


def _link_count(connection: sqlite3.Connection, note_id: str | None = None) -> int:
    if note_id:
        return connection.execute(
            "SELECT COUNT(*) FROM context_note_links WHERE note_id = ?", (note_id,)
        ).fetchone()[0]
    return connection.execute("SELECT COUNT(*) FROM context_note_links").fetchone()[0]


def _seed_activity(connection: sqlite3.Connection, activity_id: str = "activity-seed-1") -> str:
    connection.execute(
        """
        INSERT OR IGNORE INTO sync_runs (sync_run_id, source, status, started_at, finished_at)
        VALUES ('run-seed-1', 'fixture', 'completed',
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


class NotesCliAddTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    # A. Happy Path

    def test_add_persists_note_with_all_fields(self) -> None:
        result = _run_notes(
            ["add", "--type", "illness", "--body", "Mild cold", "--date", "2026-07-01"],
            self.database_path,
        )

        self.assertEqual("", result.stderr)
        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT note_kind, note_text, occurred_at FROM context_notes"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("illness", row["note_kind"])
        self.assertEqual("Mild cold", row["note_text"])
        self.assertIn("2026-07-01", row["occurred_at"])

    def test_add_prints_confirmation_with_note_id(self) -> None:
        result = _run_notes(
            ["add", "--type", "stress", "--body", "Deadline week"],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        self.assertNotEqual("", result.stdout.strip())

    def test_add_defaults_date_to_today_when_omitted(self) -> None:
        result = _run_notes(
            ["add", "--type", "stress", "--body", "High work week"],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT occurred_at FROM context_notes").fetchone()
        today = date.today().isoformat()
        self.assertIn(today, row["occurred_at"])

    def test_add_links_note_to_activity_when_activity_flag_given(self) -> None:
        with connect_database(self.database_path) as connection:
            activity_id = _seed_activity(connection)

        result = _run_notes(
            ["add", "--type", "injury", "--body", "Left knee", "--activity", activity_id],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM context_note_links WHERE linked_record_id = ?",
                (activity_id,),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_add_sets_user_entered_provenance(self) -> None:
        result = _run_notes(
            ["add", "--type", "note", "--body", "Easy recovery day"],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            note_id = connection.execute(
                "SELECT record_id FROM context_notes"
            ).fetchone()["record_id"]
            row = connection.execute(
                "SELECT provenance_kind FROM records WHERE record_id = ?", (note_id,)
            ).fetchone()
        self.assertEqual("user_entered", row["provenance_kind"])

    # B. Boundary Values

    def test_add_body_single_character(self) -> None:
        result = _run_notes(
            ["add", "--type", "note", "--body", "x"],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            self.assertEqual(1, _note_count(connection))

    def test_add_body_at_max_expected_length(self) -> None:
        long_body = "A" * 2000
        result = _run_notes(
            ["add", "--type", "note", "--body", long_body],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            stored = connection.execute(
                "SELECT note_text FROM context_notes"
            ).fetchone()["note_text"]
        self.assertEqual(long_body, stored)

    # C. Equivalence Partitioning — valid type classes

    def test_add_type_illness_accepted(self) -> None:
        result = _run_notes(["add", "--type", "illness", "--body", "Flu"], self.database_path)
        self.assertEqual(0, result.returncode)

    def test_add_type_injury_accepted(self) -> None:
        result = _run_notes(["add", "--type", "injury", "--body", "Shin splints"], self.database_path)
        self.assertEqual(0, result.returncode)

    def test_add_type_travel_accepted(self) -> None:
        result = _run_notes(["add", "--type", "travel", "--body", "Boston trip"], self.database_path)
        self.assertEqual(0, result.returncode)

    def test_add_type_stress_accepted(self) -> None:
        result = _run_notes(["add", "--type", "stress", "--body", "Work crunch"], self.database_path)
        self.assertEqual(0, result.returncode)

    def test_add_type_note_accepted(self) -> None:
        result = _run_notes(["add", "--type", "note", "--body", "General observation"], self.database_path)
        self.assertEqual(0, result.returncode)

    def test_add_rejects_unknown_type(self) -> None:
        result = _run_notes(
            ["add", "--type", "hangover", "--body", "Weekend"],
            self.database_path,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)
        # Should tell user what the valid choices are
        error_output = result.stderr + result.stdout
        self.assertTrue(
            any(kind in error_output for kind in ("illness", "injury", "travel", "stress", "note")),
            f"Expected valid type choices in output, got: {error_output!r}",
        )

    def test_add_rejects_ambiguous_date_format(self) -> None:
        result = _run_notes(
            ["add", "--type", "note", "--body", "Test", "--date", "03/07/2026"],
            self.database_path,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    # D. Edge Cases

    def test_add_body_with_newlines_and_unicode(self) -> None:
        body = "Felt great\nPR attempt — 42k"
        result = _run_notes(
            ["add", "--type", "note", "--body", body],
            self.database_path,
        )

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            stored = connection.execute(
                "SELECT note_text FROM context_notes"
            ).fetchone()["note_text"]
        self.assertEqual(body, stored)

    def test_add_body_with_only_whitespace_is_rejected(self) -> None:
        result = _run_notes(
            ["add", "--type", "note", "--body", "   "],
            self.database_path,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    # E. Error / Negative Tests

    def test_add_missing_body_flag_exits_with_argparse_error(self) -> None:
        result = _run_notes(["add", "--type", "illness"], self.database_path)

        self.assertEqual(2, result.returncode)
        self.assertIn("--body", result.stderr)

    def test_add_missing_type_flag_exits_with_argparse_error(self) -> None:
        result = _run_notes(["add", "--body", "sore"], self.database_path)

        self.assertEqual(2, result.returncode)
        self.assertIn("--type", result.stderr)

    def test_add_activity_flag_with_nonexistent_activity_id_is_rejected(self) -> None:
        result = _run_notes(
            ["add", "--type", "injury", "--body", "Knee pain", "--activity", "fake-activity-id"],
            self.database_path,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_database_path_placeholder_rejected_without_traceback(self) -> None:
        env = _pythonpath_env()
        env[DATABASE_PATH_ENV] = "/absolute/path/to/trainingos.sqlite3"

        result = subprocess.run(
            [sys.executable, "-m", "trainingos.notes", "list"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("documentation placeholder", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class NotesCliListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _add_note(self, note_type: str, body: str, date: str) -> None:
        _run_notes(
            ["add", "--type", note_type, "--body", body, "--date", date],
            self.database_path,
        )

    # A. Happy Path

    def test_list_prints_notes_in_reverse_chronological_order(self) -> None:
        self._add_note("note", "Oldest note", "2026-06-01")
        self._add_note("note", "Middle note", "2026-06-15")
        self._add_note("note", "Newest note", "2026-07-01")

        result = _run_notes(["list"], self.database_path)

        self.assertEqual(0, result.returncode)
        newest_pos = result.stdout.index("Newest note")
        middle_pos = result.stdout.index("Middle note")
        oldest_pos = result.stdout.index("Oldest note")
        self.assertLess(newest_pos, middle_pos)
        self.assertLess(middle_pos, oldest_pos)

    def test_list_filters_by_type(self) -> None:
        self._add_note("illness", "Flu symptoms", "2026-07-01")
        self._add_note("stress", "Deadline crunch", "2026-07-02")

        result = _run_notes(["list", "--type", "illness"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertIn("Flu symptoms", result.stdout)
        self.assertNotIn("Deadline crunch", result.stdout)

    def test_list_filters_by_since_date(self) -> None:
        self._add_note("note", "June note", "2026-06-01")
        self._add_note("note", "July note", "2026-07-01")

        result = _run_notes(["list", "--since", "2026-07-01"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertIn("July note", result.stdout)
        self.assertNotIn("June note", result.stdout)

    # B. Boundary Values

    def test_list_since_on_exact_note_date_is_inclusive(self) -> None:
        self._add_note("note", "Boundary note", "2026-07-01")

        result = _run_notes(["list", "--since", "2026-07-01"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertIn("Boundary note", result.stdout)

    def test_list_since_one_day_after_only_note_returns_empty(self) -> None:
        self._add_note("note", "Missed note", "2026-07-01")

        result = _run_notes(["list", "--since", "2026-07-02"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertNotIn("Missed note", result.stdout)

    # D. Edge Cases

    def test_list_when_no_notes_exist_exits_zero_with_empty_output(self) -> None:
        result = _run_notes(["list"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_list_returns_both_notes_on_same_date(self) -> None:
        self._add_note("note", "Morning note", "2026-07-01")
        self._add_note("note", "Evening note", "2026-07-01")

        result = _run_notes(["list"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertIn("Morning note", result.stdout)
        self.assertIn("Evening note", result.stdout)


class NotesCliDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _add_and_get_id(self, note_type: str = "note", body: str = "Test note") -> str:
        result = _run_notes(
            ["add", "--type", note_type, "--body", body, "--date", "2026-07-01"],
            self.database_path,
        )
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT record_id FROM context_notes WHERE note_text = ?", (body,)
            ).fetchone()
        if row is None:
            self.fail(
                f"Note was not persisted by 'notes add' (exit {result.returncode}).\n"
                f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
            )
        return row["record_id"]

    # A. Happy Path

    def test_delete_removes_note_by_id(self) -> None:
        note_id = self._add_and_get_id(body="Note to delete")

        result = _run_notes(["delete", note_id], self.database_path)

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM context_notes WHERE record_id = ?", (note_id,)
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_delete_last_remaining_note_leaves_empty_table(self) -> None:
        note_id = self._add_and_get_id(body="Only note")
        _run_notes(["delete", note_id], self.database_path)

        result = _run_notes(["list"], self.database_path)

        self.assertEqual(0, result.returncode)
        self.assertNotIn("Only note", result.stdout)

    def test_delete_note_with_linked_activity_also_removes_link_rows(self) -> None:
        with connect_database(self.database_path) as connection:
            activity_id = _seed_activity(connection)
        _run_notes(
            ["add", "--type", "injury", "--body", "Knee pain", "--activity", activity_id, "--date", "2026-07-01"],
            self.database_path,
        )
        with connect_database(self.database_path) as connection:
            note_id = connection.execute("SELECT record_id FROM context_notes").fetchone()["record_id"]

        result = _run_notes(["delete", note_id], self.database_path)

        self.assertEqual(0, result.returncode)
        with connect_database(self.database_path) as connection:
            note_count = _note_count(connection)
            link_count = _link_count(connection, note_id)
            # Activity record must be untouched
            activity_count = connection.execute(
                "SELECT COUNT(*) FROM activities WHERE record_id = ?", (activity_id,)
            ).fetchone()[0]
        self.assertEqual(0, note_count)
        self.assertEqual(0, link_count)
        self.assertEqual(1, activity_count)

    # E. Error / Negative Tests

    def test_delete_nonexistent_id_exits_nonzero_with_clear_message(self) -> None:
        result = _run_notes(["delete", "no-such-id"], self.database_path)

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)
        error_output = result.stderr + result.stdout
        self.assertTrue(
            "not found" in error_output.lower() or "no-such-id" in error_output,
            f"Expected 'not found' message, got: {error_output!r}",
        )


if __name__ == "__main__":
    unittest.main()
