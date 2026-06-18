from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trainingos.config import DATABASE_PATH_ENV


class StorageCliTests(unittest.TestCase):
    def test_module_initializes_explicit_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database_path = Path(root) / "nested" / "training.sqlite3"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trainingos.storage",
                    "--database",
                    str(database_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=_pythonpath_env(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertTrue(database_path.exists())
            self.assertIn("Database ready at", result.stdout)

    def test_module_rejects_placeholder_database_path_without_traceback(self) -> None:
        env = _pythonpath_env()
        env[DATABASE_PATH_ENV] = "/absolute/path/to/trainingos.sqlite3"

        result = subprocess.run(
            [sys.executable, "-m", "trainingos.storage"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("documentation placeholder", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else src_path
    )
    return env


if __name__ == "__main__":
    unittest.main()
