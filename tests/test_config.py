from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from trainingos.config import AppConfig, DATABASE_PATH_ENV


class AppConfigTests(unittest.TestCase):
    def test_database_path_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env({DATABASE_PATH_ENV: "var/training.sqlite3"})

        self.assertEqual(
            Path("var/training.sqlite3").absolute(),
            config.database_path,
        )

    def test_database_path_defaults_to_local_user_data_directory(self) -> None:
        with patch("pathlib.Path.home", return_value=Path("/home/runner")):
            config = AppConfig.from_env({})

        self.assertEqual(
            Path("/home/runner/.local/share/trainingos/trainingos.sqlite3"),
            config.database_path,
        )


if __name__ == "__main__":
    unittest.main()
