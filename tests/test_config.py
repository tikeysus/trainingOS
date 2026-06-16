from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from trainingos.config import (
    AppConfig,
    DATABASE_PATH_ENV,
    LOCAL_TIMEZONE_ENV,
    RAW_DATA_DIR_ENV,
)


class AppConfigTests(unittest.TestCase):
    def test_database_path_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env({DATABASE_PATH_ENV: "var/training.sqlite3"})

        self.assertEqual(
            Path("var/training.sqlite3").absolute(),
            config.database_path,
        )
        self.assertEqual(
            Path.home() / ".local/share/trainingos/raw",
            config.raw_data_dir,
        )
        self.assertEqual("UTC", config.local_timezone)

    def test_database_path_defaults_to_local_user_data_directory(self) -> None:
        with patch("pathlib.Path.home", return_value=Path("/home/runner")):
            config = AppConfig.from_env({})

        self.assertEqual(
            Path("/home/runner/.local/share/trainingos/trainingos.sqlite3"),
            config.database_path,
        )
        self.assertEqual(
            Path("/home/runner/.local/share/trainingos/raw"),
            config.raw_data_dir,
        )

    def test_raw_data_dir_and_timezone_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env(
            {
                RAW_DATA_DIR_ENV: "var/raw",
                LOCAL_TIMEZONE_ENV: "America/Toronto",
            }
        )

        self.assertEqual(Path("var/raw").absolute(), config.raw_data_dir)
        self.assertEqual("America/Toronto", config.local_timezone)

    def test_local_timezone_must_be_valid_iana_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid IANA timezone"):
            AppConfig.from_env({LOCAL_TIMEZONE_ENV: "Toronto"})


if __name__ == "__main__":
    unittest.main()
