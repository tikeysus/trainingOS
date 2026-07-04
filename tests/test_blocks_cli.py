"""Tests for training block lifecycle CLI (trainingos.blocks)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.blocks import BlockRow, close_block, create_block, list_blocks, set_phase
from trainingos.storage import apply_migrations, connect_database

_TIMEZONE = "America/Toronto"
_TODAY = date(2026, 7, 3)
_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class BlocksLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "training.sqlite3"
        self.connection = connect_database(self.db_path)
        apply_migrations(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmpdir.cleanup()

    # ---- A. Happy Path -------------------------------------------------------

    def test_create_block_persists_goal_and_start_date(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Hamilton Marathon build",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        self.assertIsNotNone(block_id)
        self.assertTrue(block_id.strip())
        row = self.connection.execute(
            """
            SELECT tb.goal, tb.start_date, tb.ended_at, r.provenance_kind
            FROM training_blocks AS tb
            JOIN records AS r USING (record_id)
            WHERE tb.record_id = ?
            """,
            (block_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("Hamilton Marathon build", row["goal"])
        self.assertEqual("2026-06-01", row["start_date"])
        self.assertIsNone(row["ended_at"])
        self.assertEqual("user_entered", row["provenance_kind"])

    def test_create_block_with_race_date_stores_race_date(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Sub-3 build",
            start_date=date(2026, 6, 1),
            race_date=date(2026, 10, 18),
            now=_NOW,
        )

        row = self.connection.execute(
            "SELECT race_date FROM training_blocks WHERE record_id = ?",
            (block_id,),
        ).fetchone()
        self.assertEqual("2026-10-18", row["race_date"])

    def test_create_block_with_initial_phase_records_first_transition(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Base phase start",
            start_date=date(2026, 6, 1),
            initial_phase="base",
            now=_NOW,
        )

        row = self.connection.execute(
            "SELECT phase, recorded_at FROM block_phase_transitions WHERE block_id = ?",
            (block_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("base", row["phase"])
        recorded_at = datetime.fromisoformat(row["recorded_at"])
        self.assertIsNotNone(recorded_at.tzinfo)

    def test_close_block_sets_ended_at(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Marathon prep",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        close_block(self.connection, now=_NOW)

        row = self.connection.execute(
            "SELECT ended_at FROM training_blocks WHERE record_id = ?",
            (block_id,),
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])
        ended_at = datetime.fromisoformat(row["ended_at"])
        self.assertIsNotNone(ended_at.tzinfo)

    def test_list_blocks_returns_all_blocks_ordered_by_start_date_desc(self) -> None:
        create_block(self.connection, goal="Early block", start_date=date(2026, 3, 1), now=_NOW)
        close_block(self.connection, now=_NOW)
        create_block(self.connection, goal="Later block", start_date=date(2026, 6, 1), now=_NOW)

        blocks = list_blocks(self.connection)

        self.assertEqual(2, len(blocks))
        self.assertEqual("Later block", blocks[0].goal)
        self.assertEqual("Early block", blocks[1].goal)

    def test_set_phase_appends_transition_with_utc_timestamp(self) -> None:
        create_block(self.connection, goal="Build block", start_date=date(2026, 6, 1), now=_NOW)

        set_phase(self.connection, "build", now=_NOW)

        row = self.connection.execute(
            "SELECT recorded_at FROM block_phase_transitions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        ts = datetime.fromisoformat(row["recorded_at"])
        self.assertEqual(UTC, ts.tzinfo)

    def test_list_blocks_after_close_shows_ended_at_populated(self) -> None:
        create_block(self.connection, goal="Closed block", start_date=date(2026, 6, 1), now=_NOW)
        close_block(self.connection, now=_NOW)

        blocks = list_blocks(self.connection)

        self.assertEqual(1, len(blocks))
        self.assertIsNotNone(blocks[0].ended_at)

    # ---- B. Boundary Value Analysis ------------------------------------------

    def test_create_block_race_date_same_as_start_date_is_allowed(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Same-day race",
            start_date=date(2026, 9, 1),
            race_date=date(2026, 9, 1),
            now=_NOW,
        )
        self.assertIsNotNone(block_id)

    def test_close_block_by_explicit_block_id_closes_target(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Explicit close",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        close_block(self.connection, block_id=block_id, now=_NOW)

        row = self.connection.execute(
            "SELECT ended_at FROM training_blocks WHERE record_id = ?",
            (block_id,),
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_create_block_with_all_phase_values_accepted(self) -> None:
        for phase in ("base", "build", "peak", "taper"):
            with tempfile.TemporaryDirectory() as tmpdir:
                conn = connect_database(Path(tmpdir) / "t.sqlite3")
                apply_migrations(conn)
                block_id = create_block(
                    conn,
                    goal=f"{phase} phase block",
                    start_date=date(2026, 6, 1),
                    initial_phase=phase,
                    now=_NOW,
                )
                self.assertIsNotNone(block_id, f"create_block failed for phase={phase}")
                conn.close()

    def test_phase_transitions_timestamps_are_ordered_by_insertion(self) -> None:
        create_block(self.connection, goal="Two phases", start_date=date(2026, 6, 1), now=_NOW)
        t1 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)

        set_phase(self.connection, "base", now=t1)
        set_phase(self.connection, "build", now=t2)

        rows = self.connection.execute(
            "SELECT recorded_at FROM block_phase_transitions ORDER BY rowid"
        ).fetchall()
        self.assertEqual(2, len(rows))
        ts0 = datetime.fromisoformat(rows[0]["recorded_at"])
        ts1 = datetime.fromisoformat(rows[1]["recorded_at"])
        self.assertLessEqual(ts0, ts1)

    # ---- C. Equivalence Partitioning -----------------------------------------

    def test_create_block_without_initial_phase_leaves_transitions_empty(self) -> None:
        block_id = create_block(
            self.connection,
            goal="No-phase block",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        count = self.connection.execute(
            "SELECT COUNT(*) FROM block_phase_transitions WHERE block_id = ?",
            (block_id,),
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_close_block_with_no_block_id_closes_active_block(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Implicitly closed",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        close_block(self.connection, now=_NOW)

        row = self.connection.execute(
            "SELECT ended_at FROM training_blocks WHERE record_id = ?",
            (block_id,),
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_set_phase_with_no_block_id_targets_active_block(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Target block",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        set_phase(self.connection, "peak", now=_NOW)

        row = self.connection.execute(
            "SELECT block_id FROM block_phase_transitions WHERE phase = 'peak'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(block_id, row["block_id"])

    # ---- D. Edge Cases -------------------------------------------------------

    def test_list_blocks_with_empty_database_returns_empty_list(self) -> None:
        blocks = list_blocks(self.connection)
        self.assertEqual([], blocks)

    def test_create_close_create_cycle_allows_second_active_block(self) -> None:
        block_a = create_block(
            self.connection,
            goal="Block A",
            start_date=date(2026, 3, 1),
            now=_NOW,
        )
        close_block(self.connection, now=_NOW)
        block_b = create_block(
            self.connection,
            goal="Block B",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        blocks = list_blocks(self.connection)
        self.assertEqual(2, len(blocks))
        row_a = self.connection.execute(
            "SELECT ended_at FROM training_blocks WHERE record_id = ?", (block_a,)
        ).fetchone()
        row_b = self.connection.execute(
            "SELECT ended_at FROM training_blocks WHERE record_id = ?", (block_b,)
        ).fetchone()
        self.assertIsNotNone(row_a["ended_at"])
        self.assertIsNone(row_b["ended_at"])

    def test_multiple_phase_transitions_all_persisted_in_order(self) -> None:
        create_block(self.connection, goal="Full cycle", start_date=date(2026, 6, 1), now=_NOW)

        set_phase(self.connection, "base", now=datetime(2026, 6, 1, tzinfo=UTC))
        set_phase(self.connection, "build", now=datetime(2026, 6, 15, tzinfo=UTC))
        set_phase(self.connection, "peak", now=datetime(2026, 7, 1, tzinfo=UTC))

        rows = self.connection.execute(
            "SELECT phase FROM block_phase_transitions ORDER BY rowid"
        ).fetchall()
        self.assertEqual(["base", "build", "peak"], [r["phase"] for r in rows])

    def test_list_blocks_returns_current_phase_from_most_recent_transition(self) -> None:
        create_block(self.connection, goal="Phase progression", start_date=date(2026, 6, 1), now=_NOW)
        set_phase(self.connection, "base", now=datetime(2026, 6, 1, tzinfo=UTC))
        set_phase(self.connection, "build", now=datetime(2026, 6, 15, tzinfo=UTC))

        blocks = list_blocks(self.connection)

        self.assertEqual(1, len(blocks))
        self.assertEqual("build", blocks[0].current_phase)

    # ---- E. Error / Negative Tests -------------------------------------------

    def test_create_block_when_active_block_exists_raises_value_error(self) -> None:
        create_block(
            self.connection,
            goal="Active block",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        with self.assertRaisesRegex(ValueError, "active"):
            create_block(
                self.connection,
                goal="Duplicate active",
                start_date=date(2026, 7, 1),
                now=_NOW,
            )

    def test_create_block_with_blank_goal_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "goal"):
            create_block(
                self.connection,
                goal="   ",
                start_date=date(2026, 6, 1),
                now=_NOW,
            )

    def test_create_block_race_date_before_start_date_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            create_block(
                self.connection,
                goal="Bad dates",
                start_date=date(2026, 9, 1),
                race_date=date(2026, 8, 31),
                now=_NOW,
            )

    def test_close_block_with_no_active_block_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            close_block(self.connection, now=_NOW)

    def test_close_already_closed_block_raises_value_error(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Already closed",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )
        close_block(self.connection, now=_NOW)

        with self.assertRaises(ValueError):
            close_block(self.connection, block_id=block_id, now=_NOW)

    def test_set_phase_with_invalid_phase_name_raises(self) -> None:
        create_block(
            self.connection,
            goal="Sprint block",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        with self.assertRaises((ValueError, KeyError)):
            set_phase(self.connection, "sprint", now=_NOW)

    def test_set_phase_with_no_active_block_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            set_phase(self.connection, "base", now=_NOW)

    def test_close_block_with_nonexistent_block_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            close_block(self.connection, block_id="does-not-exist", now=_NOW)

    # ---- F. State-Based Tests ------------------------------------------------

    def test_newly_created_block_is_considered_active(self) -> None:
        create_block(
            self.connection,
            goal="Should be active",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )

        close_block(self.connection, now=_NOW)

        blocks = list_blocks(self.connection)
        self.assertIsNotNone(blocks[0].ended_at)

    def test_closed_block_is_not_targetable_by_set_phase_without_block_id(self) -> None:
        create_block(
            self.connection,
            goal="Closed block",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )
        close_block(self.connection, now=_NOW)

        with self.assertRaises(ValueError):
            set_phase(self.connection, "taper", now=_NOW)

    def test_set_phase_on_closed_block_by_explicit_id_raises_value_error(self) -> None:
        block_id = create_block(
            self.connection,
            goal="Closed",
            start_date=date(2026, 6, 1),
            now=_NOW,
        )
        close_block(self.connection, now=_NOW)

        with self.assertRaises(ValueError):
            set_phase(self.connection, "taper", block_id=block_id, now=_NOW)


class BlocksCliSubprocessTests(unittest.TestCase):
    def test_cli_start_subcommand_exits_zero_and_prints_block_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "training.sqlite3"

            result = subprocess.run(
                [
                    sys.executable, "-m", "trainingos.blocks",
                    "start",
                    "--goal", "Hamilton Marathon build",
                    "--database", str(db_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=_pythonpath_env(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertTrue(result.stdout.strip())

    def test_cli_list_subcommand_exits_zero_with_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "training.sqlite3"

            result = subprocess.run(
                [
                    sys.executable, "-m", "trainingos.blocks",
                    "list",
                    "--database", str(db_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=_pythonpath_env(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode)
            self.assertEqual("", result.stderr)

    def test_cli_close_subcommand_exits_zero_for_active_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "training.sqlite3"
            conn = connect_database(db_path)
            apply_migrations(conn)
            create_block(conn, goal="Build phase", start_date=date(2026, 6, 1), now=_NOW)
            conn.close()

            result = subprocess.run(
                [
                    sys.executable, "-m", "trainingos.blocks",
                    "close",
                    "--database", str(db_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=_pythonpath_env(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)

    def test_cli_phase_set_invalid_phase_exits_nonzero_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "training.sqlite3"

            result = subprocess.run(
                [
                    sys.executable, "-m", "trainingos.blocks",
                    "phase", "set", "badphase",
                    "--database", str(db_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=_pythonpath_env(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertNotIn("Traceback", result.stderr)


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
    return env


if __name__ == "__main__":
    unittest.main()
