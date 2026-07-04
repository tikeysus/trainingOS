import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# Ensure we import from the correct project
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trainingos.storage import apply_migrations, connect_database


class MigrationViewConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "training.sqlite3"
        self.connection = connect_database(self.db_path)
        apply_migrations(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmpdir.cleanup()

    # ---- A. Happy Path ----

    def test_dashboard_block_comparison_view_queryable_after_all_migrations(self) -> None:
        result = self.connection.execute("SELECT * FROM dashboard_block_comparison LIMIT 1").fetchall()
        self.assertIsNotNone(result)

    def test_dashboard_context_annotations_view_queryable_after_all_migrations(self) -> None:
        result = self.connection.execute("SELECT * FROM dashboard_context_annotations LIMIT 1").fetchall()
        self.assertIsNotNone(result)

    # ---- F. State/Integration Tests ----

    def test_both_views_return_rows_when_block_exists(self) -> None:
        block_id = "block:test:001"
        now_iso = datetime.now().isoformat()

        self.connection.execute(
            """
            INSERT INTO records (record_id, record_type, timezone, created_at, updated_at)
            VALUES (?, 'training_block', 'America/Toronto', ?, ?)
            """,
            (block_id, now_iso, now_iso),
        )

        self.connection.execute(
            """
            INSERT INTO training_blocks (record_id, goal, start_date)
            VALUES (?, 'Test Block', '2026-01-01')
            """,
            (block_id,),
        )

        self.connection.commit()

        comparison_rows = self.connection.execute(
            "SELECT * FROM dashboard_block_comparison"
        ).fetchall()
        self.assertGreaterEqual(len(comparison_rows), 1)

        annotation_rows = self.connection.execute(
            "SELECT * FROM dashboard_context_annotations WHERE annotation_type = 'training_block'"
        ).fetchall()
        self.assertGreaterEqual(len(annotation_rows), 1)


if __name__ == "__main__":
    unittest.main()
