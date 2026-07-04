from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from trainingos.domain import (
    Activity,
    ActivityType,
    Measurement,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.notes import main
from trainingos.storage import apply_migrations, connect_database


class NotesAddTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_add_persists_illness_note_with_user_entered_provenance(self) -> None:
        exit_code = main(self._db_argv() + ["add", "--type", "illness", "--body", "Head cold, skipped long run"])

        self.assertEqual(0, exit_code)
        self.assertEqual(1, self._count_notes())
        row = self._note_row()
        self.assertEqual("illness", row["note_kind"])
        self.assertEqual("Head cold, skipped long run", row["note_text"])
        with connect_database(self.database_path) as connection:
            record = connection.execute(
                "SELECT provenance_kind FROM records WHERE record_id = ?",
                (row["record_id"],),
            ).fetchone()
        self.assertEqual("user_entered", record["provenance_kind"])

    def test_add_with_explicit_date_stores_occurred_at_on_that_date(self) -> None:
        exit_code = main(self._db_argv() + ["add", "--type", "travel", "--body", "Boston trip", "--date", "2026-06-01"])

        self.assertEqual(0, exit_code)
        row = self._note_row()
        self.assertTrue(row["occurred_at"].startswith("2026-06-01"), row["occurred_at"])

    def test_add_without_date_defaults_to_today(self) -> None:
        today_str = date.today().isoformat()

        exit_code = main(self._db_argv() + ["add", "--type", "note", "--body", "General note"])

        self.assertEqual(0, exit_code)
        row = self._note_row()
        self.assertTrue(row["occurred_at"].startswith(today_str), row["occurred_at"])

    def test_add_with_activity_link_inserts_context_note_link(self) -> None:
        self._seed_activity("activity-1")

        exit_code = main(self._db_argv() + ["add", "--type", "injury", "--body", "Calf tightness", "--activity", "activity-1"])

        self.assertEqual(0, exit_code)
        self.assertEqual(1, self._link_count())
        with connect_database(self.database_path) as connection:
            link = connection.execute("SELECT linked_record_id FROM context_note_links").fetchone()
        self.assertEqual("activity-1", link["linked_record_id"])

    def test_add_without_activity_link_leaves_link_table_empty(self) -> None:
        main(self._db_argv() + ["add", "--type", "stress", "--body", "Pre-race nerves"])
        self.assertEqual(0, self._link_count())

    def test_add_body_single_character_is_accepted(self) -> None:
        exit_code = main(self._db_argv() + ["add", "--type", "note", "--body", "X"])
        self.assertEqual(0, exit_code)
        self.assertEqual(1, self._count_notes())

    def test_add_body_very_long_text_is_stored_intact(self) -> None:
        long_body = "A" * 10_000

        exit_code = main(self._db_argv() + ["add", "--type", "note", "--body", long_body])

        self.assertEqual(0, exit_code)
        self.assertEqual(long_body, self._note_row()["note_text"])

    def test_all_valid_note_types_are_accepted(self) -> None:
        for kind in ("illness", "injury", "travel", "stress", "note"):
            with self.subTest(kind=kind):
                with connect_database(self.database_path) as connection:
                    connection.execute("DELETE FROM context_note_links")
                    connection.execute("DELETE FROM context_notes")
                    connection.execute("DELETE FROM records WHERE record_type = 'context_note'")
                exit_code = main(self._db_argv() + ["add", "--type", kind, "--body", f"{kind} body"])
                self.assertEqual(0, exit_code, f"Expected success for type {kind!r}")
                row = self._note_row()
                self.assertEqual(kind, row["note_kind"])

    def test_add_invalid_type_exits_with_error_and_no_row(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            exit_code = main(self._db_argv() + ["add", "--type", "hangover", "--body", "bad input"])

        self.assertEqual(2, exit_code)
        output = mock_err.getvalue()
        self.assertTrue(
            any(t in output for t in ("illness", "injury", "travel", "stress", "note")),
            f"Expected valid types listed in error output, got: {output!r}",
        )
        self.assertEqual(0, self._count_notes())

    def test_add_missing_body_exits_with_error(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            exit_code = main(self._db_argv() + ["add", "--type", "illness"])

        self.assertEqual(2, exit_code)
        self.assertIn("body", mock_err.getvalue().lower())

    def test_add_invalid_date_format_exits_with_error_and_no_row(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            exit_code = main(self._db_argv() + ["add", "--type", "note", "--body", "test", "--date", "July 4th"])

        self.assertEqual(2, exit_code)
        output = mock_err.getvalue()
        self.assertIn("YYYY-MM-DD", output)
        self.assertEqual(0, self._count_notes())

    def test_add_activity_link_to_nonexistent_record_exits_cleanly_with_no_note_row(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            exit_code = main(self._db_argv() + ["add", "--type", "injury", "--body", "test", "--activity", "missing-99"])

        self.assertNotEqual(0, exit_code)
        self.assertNotIn("Traceback", mock_err.getvalue())
        self.assertEqual(0, self._count_notes())

    def _db_argv(self) -> list[str]:
        return ["--database", str(self.database_path)]

    def _count_notes(self) -> int:
        with connect_database(self.database_path) as connection:
            return connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]

    def _note_row(self):
        with connect_database(self.database_path) as connection:
            return connection.execute("SELECT * FROM context_notes").fetchone()

    def _link_count(self) -> int:
        with connect_database(self.database_path) as connection:
            return connection.execute("SELECT COUNT(*) FROM context_note_links").fetchone()[0]

    def _seed_activity(self, record_id: str) -> None:
        timestamp = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        with connect_database(self.database_path) as connection:
            store = NormalizationStore(connection)
            store.upsert_activity(
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
            connection.commit()


class NotesListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_list_with_no_notes_exits_zero_with_no_output(self) -> None:
        exit_code, output = self._run_list()
        self.assertEqual(0, exit_code)
        self.assertEqual("", output.strip())

    def test_list_returns_notes_newest_first(self) -> None:
        self._add_note("illness", "Oldest note", "2026-06-01")
        self._add_note("injury", "Middle note", "2026-06-10")
        self._add_note("travel", "Newest note", "2026-06-15")

        exit_code, output = self._run_list()

        self.assertEqual(0, exit_code)
        pos_newest = output.find("2026-06-15")
        pos_middle = output.find("2026-06-10")
        pos_oldest = output.find("2026-06-01")
        self.assertGreater(pos_newest, -1)
        self.assertLess(pos_newest, pos_middle)
        self.assertLess(pos_middle, pos_oldest)

    def test_list_includes_note_kind_and_body_in_output(self) -> None:
        self._add_note("illness", "High fever, DNS", "2026-06-10")

        exit_code, output = self._run_list()

        self.assertEqual(0, exit_code)
        self.assertIn("illness", output)
        self.assertIn("High fever, DNS", output)

    def test_list_type_filter_returns_only_matching_kind(self) -> None:
        self._add_note("illness", "Sick note", "2026-06-10")
        self._add_note("injury", "Injury note", "2026-06-11")

        exit_code, output = self._run_list(["--type", "illness"])

        self.assertEqual(0, exit_code)
        self.assertIn("Sick note", output)
        self.assertNotIn("Injury note", output)

    def test_list_type_filter_returns_empty_when_no_match(self) -> None:
        self._add_note("illness", "Cold", "2026-06-10")

        exit_code, output = self._run_list(["--type", "stress"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", output.strip())

    def test_list_since_excludes_older_notes(self) -> None:
        self._add_note("injury", "Old knee pain", "2026-05-01")
        self._add_note("injury", "Recent knee pain", "2026-06-15")

        exit_code, output = self._run_list(["--since", "2026-06-01"])

        self.assertEqual(0, exit_code)
        self.assertIn("Recent knee pain", output)
        self.assertNotIn("Old knee pain", output)

    def test_list_since_on_exact_note_date_includes_note(self) -> None:
        self._add_note("travel", "Boston trip", "2026-06-15")

        exit_code, output = self._run_list(["--since", "2026-06-15"])

        self.assertEqual(0, exit_code)
        self.assertIn("Boston trip", output)

    def test_list_since_one_day_after_note_excludes_note(self) -> None:
        self._add_note("travel", "Boston trip", "2026-06-15")

        exit_code, output = self._run_list(["--since", "2026-06-16"])

        self.assertEqual(0, exit_code)
        self.assertNotIn("Boston trip", output)

    def test_list_type_and_since_combined_filters(self) -> None:
        self._add_note("injury", "Old injury", "2026-05-01")
        self._add_note("injury", "Recent injury", "2026-06-15")
        self._add_note("illness", "Recent illness", "2026-06-20")

        exit_code, output = self._run_list(["--type", "injury", "--since", "2026-06-01"])

        self.assertEqual(0, exit_code)
        self.assertIn("Recent injury", output)
        self.assertNotIn("Old injury", output)
        self.assertNotIn("Recent illness", output)

    def test_list_since_boundary_exclusive_at_second_before_date(self) -> None:
        self._add_note("travel", "Day before", "2026-06-14")
        self._add_note("travel", "Target day", "2026-06-15")

        exit_code, output = self._run_list(["--since", "2026-06-15"])

        self.assertEqual(0, exit_code)
        self.assertNotIn("Day before", output, "--since should use >= not >")
        self.assertIn("Target day", output)

    def test_list_output_includes_record_id_for_copying_to_delete(self) -> None:
        self._add_note("illness", "Head cold", "2026-06-10")

        exit_code, output = self._run_list()

        self.assertEqual(0, exit_code)
        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT record_id FROM context_notes").fetchone()
        record_id = row["record_id"]
        self.assertIn(record_id, output, "record_id must appear in list output for user to copy")

    def _db_argv(self) -> list[str]:
        return ["--database", str(self.database_path)]

    def _add_note(self, kind: str, body: str, date_str: str) -> None:
        main(self._db_argv() + ["add", "--type", kind, "--body", body, "--date", date_str])

    def _run_list(self, extra_args: list[str] | None = None) -> tuple[int, str]:
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = main(self._db_argv() + ["list"] + (extra_args or []))
        return exit_code, mock_out.getvalue()


class NotesDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_delete_removes_note_and_its_records_row(self) -> None:
        record_id = self._add_and_get_record_id()

        exit_code = main(self._db_argv() + ["delete", record_id])

        self.assertEqual(0, exit_code)
        with connect_database(self.database_path) as connection:
            note_count = connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
            record_count = connection.execute(
                "SELECT COUNT(*) FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()[0]
        self.assertEqual(0, note_count)
        self.assertEqual(0, record_count)

    def test_delete_nonexistent_id_exits_with_not_found_error(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            exit_code = main(self._db_argv() + ["delete", "unknown-id-999"])

        self.assertNotEqual(0, exit_code)
        self.assertIn("not found", mock_err.getvalue().lower())

    def test_delete_does_not_affect_other_notes(self) -> None:
        first_id = self._add_and_get_record_id(body="Keep this one")
        second_id = self._add_and_get_record_id(body="Delete this one")

        main(self._db_argv() + ["delete", second_id])

        with connect_database(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
            remaining_id = connection.execute(
                "SELECT record_id FROM context_notes"
            ).fetchone()["record_id"]
        self.assertEqual(1, count)
        self.assertEqual(first_id, remaining_id)

    def test_delete_removes_orphan_context_notes_row_not_just_records(self) -> None:
        record_id = self._add_and_get_record_id()

        main(self._db_argv() + ["delete", record_id])

        with connect_database(self.database_path) as connection:
            orphan_count = connection.execute(
                "SELECT COUNT(*) FROM context_notes WHERE record_id = ?",
                (record_id,),
            ).fetchone()[0]
        self.assertEqual(0, orphan_count, "context_notes row should be deleted, not left orphaned")

    def _db_argv(self) -> list[str]:
        return ["--database", str(self.database_path)]

    def _add_and_get_record_id(self, kind: str = "illness", body: str = "test note") -> str:
        main(self._db_argv() + ["add", "--type", kind, "--body", body])
        with connect_database(self.database_path) as connection:
            rows = connection.execute(
                "SELECT record_id FROM context_notes ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return rows["record_id"]


class NotesSubcommandUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_no_subcommand_prints_subcommands_and_exits_with_usage_code(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "trainingos.notes", "--database", str(self.database_path)],
            cwd=Path(__file__).resolve().parents[1],
            env=_pythonpath_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        combined = result.stdout + result.stderr
        self.assertIn("add", combined)
        self.assertIn("list", combined)
        self.assertIn("delete", combined)

    def test_invalid_type_produces_no_traceback(self) -> None:
        result = subprocess.run(
            [
                sys.executable, "-m", "trainingos.notes",
                "--database", str(self.database_path),
                "add", "--type", "hangover", "--body", "bad",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=_pythonpath_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertNotIn("Traceback", result.stderr)


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
    return env


if __name__ == "__main__":
    unittest.main()
