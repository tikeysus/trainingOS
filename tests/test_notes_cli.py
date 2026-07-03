from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

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
from trainingos.storage import apply_migrations, connect_database


class NotesCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "training.sqlite3"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    # ── A. Happy Path ──────────────────────────────────────────────────────────

    def test_add_persists_note_and_exits_zero(self) -> None:
        result = self._run(
            "add", "--type", "illness", "--body", "Sore throat.", "--date", "2026-07-01"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertNotEqual("", result.stdout.strip())

    def test_add_with_activity_link_references_existing_activity(self) -> None:
        activity_id = self._insert_activity("activity-cli-link")

        result = self._run(
            "add",
            "--type", "injury",
            "--body", "Knee pain post-run.",
            "--activity", activity_id,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        list_result = self._run("list")
        self.assertIn(activity_id, list_result.stdout)

    def test_add_without_date_defaults_to_today(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        result = self._run("add", "--type", "stress", "--body", "Big race week.")

        self.assertEqual(0, result.returncode, result.stderr)
        list_result = self._run("list")
        self.assertIn(today, list_result.stdout)

    def test_list_returns_notes_in_reverse_chronological_order(self) -> None:
        self._run("add", "--type", "note", "--body", "Older note.", "--date", "2026-06-01")
        self._run("add", "--type", "note", "--body", "Newer note.", "--date", "2026-07-01")

        result = self._run("list")

        self.assertEqual(0, result.returncode, result.stderr)
        older_pos = result.stdout.find("Older note.")
        newer_pos = result.stdout.find("Newer note.")
        self.assertGreater(older_pos, newer_pos, "newer note should appear before older note")

    def test_list_type_filter_returns_only_matching_type(self) -> None:
        self._run("add", "--type", "illness", "--body", "Flu day.", "--date", "2026-07-01")
        self._run("add", "--type", "stress", "--body", "Work stress.", "--date", "2026-07-02")

        result = self._run("list", "--type", "illness")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Flu day.", result.stdout)
        self.assertNotIn("Work stress.", result.stdout)

    def test_list_since_filter_includes_notes_on_or_after_date(self) -> None:
        self._run("add", "--type", "note", "--body", "June note.", "--date", "2026-06-01")
        self._run("add", "--type", "note", "--body", "July note.", "--date", "2026-07-01")

        result = self._run("list", "--since", "2026-07-01")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("July note.", result.stdout)
        self.assertNotIn("June note.", result.stdout)

    def test_delete_removes_note_by_record_id(self) -> None:
        add_result = self._run(
            "add", "--type", "illness", "--body", "To delete.", "--date", "2026-07-01"
        )
        record_id = add_result.stdout.strip()

        delete_result = self._run("delete", record_id)

        self.assertEqual(0, delete_result.returncode, delete_result.stderr)
        list_result = self._run("list")
        self.assertNotIn("To delete.", list_result.stdout)

    # ── B. Boundary Values ─────────────────────────────────────────────────────

    def test_add_with_single_character_body_succeeds(self) -> None:
        result = self._run("add", "--type", "note", "--body", "x", "--date", "2026-07-01")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_list_since_boundary_inclusive_includes_note_on_exact_date(self) -> None:
        self._run("add", "--type", "note", "--body", "Exact date note.", "--date", "2026-07-01")

        result = self._run("list", "--since", "2026-07-01")

        self.assertIn("Exact date note.", result.stdout)

    def test_list_since_boundary_exclusive_excludes_note_one_day_before(self) -> None:
        self._run("add", "--type", "note", "--body", "Day before.", "--date", "2026-07-01")

        result = self._run("list", "--since", "2026-07-02")

        self.assertNotIn("Day before.", result.stdout)

    # ── C. Equivalence Partitioning ────────────────────────────────────────────

    def test_add_accepts_all_five_valid_type_values(self) -> None:
        for note_type in ("illness", "injury", "travel", "stress", "note"):
            with self.subTest(note_type=note_type):
                result = self._run(
                    "add", "--type", note_type, "--body", f"Test {note_type}.", "--date", "2026-07-01"
                )
                self.assertEqual(0, result.returncode, result.stderr)

        list_result = self._run("list")
        self.assertEqual(0, list_result.returncode)
        for note_type in ("illness", "injury", "travel", "stress", "note"):
            self.assertIn(f"Test {note_type}.", list_result.stdout)

    def test_add_rejects_unknown_type_without_traceback(self) -> None:
        result = self._run("add", "--type", "workout", "--body", "Easy run.")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    # ── D. Edge Cases ──────────────────────────────────────────────────────────

    def test_list_on_empty_database_exits_zero(self) -> None:
        result = self._run("list")

        self.assertEqual(0, result.returncode, result.stderr)

    def test_delete_non_existent_id_exits_nonzero_with_error(self) -> None:
        result = self._run("delete", "note-does-not-exist")

        self.assertNotEqual(0, result.returncode)
        self.assertTrue(
            "not found" in result.stderr.lower() or "not found" in result.stdout.lower(),
            f"expected 'not found' in output; stderr={result.stderr!r} stdout={result.stdout!r}",
        )

    def test_add_whitespace_only_body_exits_nonzero(self) -> None:
        result = self._run("add", "--type", "stress", "--body", "   ")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_invalid_date_format_exits_nonzero_without_traceback(self) -> None:
        result = self._run("add", "--type", "note", "--body", "test", "--date", "not-a-date")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_list_since_invalid_date_exits_nonzero_without_traceback(self) -> None:
        result = self._run("list", "--since", "yesterday")

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    # ── E. Error / Negative Tests ──────────────────────────────────────────────

    def test_no_subcommand_exits_nonzero_with_usage(self) -> None:
        result = self._run()

        self.assertEqual(2, result.returncode)
        combined = result.stdout + result.stderr
        self.assertTrue(
            "add" in combined or "usage" in combined.lower(),
            f"expected usage hint in output; got: {combined!r}",
        )

    def test_add_missing_body_exits_nonzero_with_argparse_error(self) -> None:
        result = self._run("add", "--type", "illness")

        self.assertEqual(2, result.returncode)
        self.assertIn("--body", result.stderr)

    def test_delete_without_id_argument_exits_nonzero(self) -> None:
        result = self._run("delete")

        self.assertEqual(2, result.returncode)

    def test_add_activity_referencing_nonexistent_record_exits_nonzero_without_traceback(
        self,
    ) -> None:
        result = self._run(
            "add", "--type", "note", "--body", "test", "--activity", "no-such-record"
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    # ── F. State-Based / Idempotency ──────────────────────────────────────────

    def test_two_identical_adds_create_two_distinct_notes(self) -> None:
        self._run("add", "--type", "note", "--body", "Duplicate body.", "--date", "2026-07-01")
        self._run("add", "--type", "note", "--body", "Duplicate body.", "--date", "2026-07-01")

        list_result = self._run("list")

        self.assertEqual(2, list_result.stdout.count("Duplicate body."))

    def test_second_delete_of_already_deleted_note_exits_nonzero(self) -> None:
        add_result = self._run("add", "--type", "note", "--body", "Once only.", "--date", "2026-07-01")
        record_id = add_result.stdout.strip()

        self._run("delete", record_id)
        second_delete = self._run("delete", record_id)

        self.assertNotEqual(0, second_delete.returncode)

    # ── Helpers ───────────────────────────────────────────────────────────────

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

    def _insert_activity(self, record_id: str) -> str:
        with connect_database(self.database_path) as connection:
            store = NormalizationStore(connection)
            store.upsert_activity(
                Activity(
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
            )
            connection.commit()
        return record_id


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
    return env


if __name__ == "__main__":
    unittest.main()
