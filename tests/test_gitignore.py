from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path],
        cwd=_REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


class GitignoreHygieneTests(unittest.TestCase):
    # SQLite / database files

    def test_sqlite3_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("training.sqlite3"))

    def test_sqlite_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("training.sqlite"))

    def test_db_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("trainingos.db"))

    def test_db_wal_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("trainingos.db-wal"))

    def test_db_shm_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("trainingos.db-shm"))

    # Garmin / FIT activity files

    def test_fit_file_at_root_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("activity.fit"))

    def test_fit_file_in_imports_dir_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("imports/2024-01-01-activity.fit"))

    def test_fit_file_in_raw_dir_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("raw/activity.fit"))

    # Private / generated directories

    def test_data_directory_contents_are_ignored(self) -> None:
        self.assertTrue(_is_ignored("data/trainingos.sqlite3"))

    def test_imports_directory_contents_are_ignored(self) -> None:
        self.assertTrue(_is_ignored("imports/garmin-export.zip"))

    def test_private_directory_contents_are_ignored(self) -> None:
        self.assertTrue(_is_ignored("private/credentials.json"))

    def test_raw_directory_contents_are_ignored(self) -> None:
        self.assertTrue(_is_ignored("raw/garmin-export.zip"))

    def test_secrets_directory_contents_are_ignored(self) -> None:
        self.assertTrue(_is_ignored("secrets/api-key.txt"))

    # macOS metadata

    def test_ds_store_at_root_is_ignored(self) -> None:
        self.assertTrue(_is_ignored(".DS_Store"))

    def test_ds_store_in_subdirectory_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("src/.DS_Store"))

    # Environment / credential files

    def test_env_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored(".env"))

    def test_env_local_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored(".env.local"))

    def test_env_production_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored(".env.production"))

    # Python build artefacts

    def test_pyc_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("trainingos/storage/__init__.cpython-312.pyc"))

    def test_pycache_directory_is_ignored(self) -> None:
        self.assertTrue(_is_ignored("trainingos/storage/__pycache__/module.py"))

    def test_coverage_file_is_ignored(self) -> None:
        self.assertTrue(_is_ignored(".coverage"))

    def test_pytest_cache_is_ignored(self) -> None:
        self.assertTrue(_is_ignored(".pytest_cache/v/cache/lastfailed"))

    def test_venv_directory_contents_are_ignored(self) -> None:
        self.assertTrue(_is_ignored(".venv/lib/python3.12/site-packages/foo.py"))

    # Explicit exceptions — must NOT be ignored

    def test_env_example_is_not_ignored(self) -> None:
        self.assertFalse(_is_ignored(".env.example"))

    def test_sanitized_fit_fixture_is_not_ignored(self) -> None:
        self.assertFalse(_is_ignored("tests/fixtures/sanitized/sample.fit"))
