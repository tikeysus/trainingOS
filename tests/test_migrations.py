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

    # ---- Tests removed: dashboard views removed in favor of direct API ----


if __name__ == "__main__":
    unittest.main()
