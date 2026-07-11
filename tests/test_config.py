from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from trainingos.config import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_CHAT_MODEL_ENV,
    AppConfig,
    COACH_ALLOW_EXTERNAL_ENV,
    COACH_HOST_ENV,
    COACH_PORT_ENV,
    COACH_TOKEN_ENV,
    DATABASE_PATH_ENV,
    DEFAULT_ANTHROPIC_CHAT_MODEL,
    LOCAL_TIMEZONE_ENV,
    RAW_DATA_DIR_ENV,
)

_API_KEY_ENV = {ANTHROPIC_API_KEY_ENV: "sk-ant-test"}


class AppConfigTests(unittest.TestCase):
    def test_database_path_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env(
            {**_API_KEY_ENV, DATABASE_PATH_ENV: "var/training.sqlite3"}
        )

        self.assertEqual(
            Path("var/training.sqlite3").absolute(),
            config.database_path,
        )
        self.assertEqual(
            Path.home() / ".local/share/trainingos/raw",
            config.raw_data_dir,
        )
        self.assertEqual("UTC", config.local_timezone)
        self.assertEqual("sk-ant-test", config.anthropic_api_key)
        self.assertEqual("127.0.0.1", config.coach_host)
        self.assertEqual(8765, config.coach_port)

    def test_database_path_defaults_to_local_user_data_directory(self) -> None:
        with patch("pathlib.Path.home", return_value=Path("/home/runner")):
            config = AppConfig.from_env(_API_KEY_ENV)

        self.assertEqual(
            Path("/home/runner/.local/share/trainingos/trainingos.sqlite3"),
            config.database_path,
        )
        self.assertEqual(
            Path("/home/runner/.local/share/trainingos/raw"),
            config.raw_data_dir,
        )

    def test_database_path_rejects_documentation_placeholders(self) -> None:
        with self.assertRaisesRegex(ValueError, "documentation placeholder"):
            AppConfig.from_env(
                {**_API_KEY_ENV, DATABASE_PATH_ENV: "/absolute/path/to/trainingos.sqlite3"}
            )

        with self.assertRaisesRegex(ValueError, "documentation placeholder"):
            AppConfig.from_env({**_API_KEY_ENV, DATABASE_PATH_ENV: "/path/to/trainingos.sqlite3"})

    def test_raw_data_dir_and_timezone_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env(
            {
                **_API_KEY_ENV,
                RAW_DATA_DIR_ENV: "var/raw",
                LOCAL_TIMEZONE_ENV: "America/Toronto",
            }
        )

        self.assertEqual(Path("var/raw").absolute(), config.raw_data_dir)
        self.assertEqual("America/Toronto", config.local_timezone)

    def test_raw_data_dir_rejects_documentation_placeholder(self) -> None:
        with self.assertRaisesRegex(ValueError, "documentation placeholder"):
            AppConfig.from_env({**_API_KEY_ENV, RAW_DATA_DIR_ENV: "/path/to/raw-artifacts"})

    def test_local_timezone_must_be_valid_iana_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid IANA timezone"):
            AppConfig.from_env({**_API_KEY_ENV, LOCAL_TIMEZONE_ENV: "Toronto"})

    def test_coach_port_must_be_valid_tcp_port(self) -> None:
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, COACH_PORT_ENV):
                    AppConfig.from_env({**_API_KEY_ENV, COACH_PORT_ENV: value})

    def test_anthropic_provider_is_configured(self) -> None:
        config = AppConfig.from_env(
            {
                ANTHROPIC_API_KEY_ENV: "sk-ant-test",
                ANTHROPIC_CHAT_MODEL_ENV: "claude-opus-4-7",
                COACH_HOST_ENV: "localhost",
                COACH_PORT_ENV: "8766",
            }
        )

        self.assertEqual("sk-ant-test", config.anthropic_api_key)
        self.assertEqual("claude-opus-4-7", config.anthropic_chat_model)
        self.assertEqual("localhost", config.coach_host)
        self.assertEqual(8766, config.coach_port)

    def test_anthropic_api_key_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, ANTHROPIC_API_KEY_ENV):
            AppConfig.from_env({})

        with self.assertRaisesRegex(ValueError, ANTHROPIC_API_KEY_ENV):
            AppConfig.from_env({ANTHROPIC_API_KEY_ENV: "  "})

    def test_anthropic_provider_uses_default_model(self) -> None:
        config = AppConfig.from_env(_API_KEY_ENV)

        self.assertEqual(DEFAULT_ANTHROPIC_CHAT_MODEL, config.anthropic_chat_model)


class AppConfigGuardTests(unittest.TestCase):
    def test_default_coach_host_is_localhost(self) -> None:
        config = AppConfig.from_env(_API_KEY_ENV)

        self.assertEqual("127.0.0.1", config.coach_host)
        self.assertFalse(config.coach_allow_external)
        self.assertIsNone(config.coach_token)

    def test_non_localhost_host_requires_allow_external_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "TRAININGOS_COACH_ALLOW_EXTERNAL"):
            AppConfig.from_env({**_API_KEY_ENV, COACH_HOST_ENV: "0.0.0.0"})

    def test_network_host_with_allow_external_flag_is_accepted(self) -> None:
        config = AppConfig.from_env(
            {**_API_KEY_ENV, COACH_HOST_ENV: "0.0.0.0", COACH_ALLOW_EXTERNAL_ENV: "1"}
        )

        self.assertEqual("0.0.0.0", config.coach_host)
        self.assertTrue(config.coach_allow_external)

    def test_localhost_variants_do_not_require_allow_external(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                config = AppConfig.from_env({**_API_KEY_ENV, COACH_HOST_ENV: host})
                self.assertEqual(host, config.coach_host)

    def test_coach_token_is_read_from_environment(self) -> None:
        config = AppConfig.from_env({**_API_KEY_ENV, COACH_TOKEN_ENV: "secret-abc"})

        self.assertEqual("secret-abc", config.coach_token)

    def test_coach_token_defaults_to_none(self) -> None:
        config = AppConfig.from_env(_API_KEY_ENV)

        self.assertIsNone(config.coach_token)

    def test_coach_token_with_external_host_and_allow_flag(self) -> None:
        config = AppConfig.from_env(
            {
                **_API_KEY_ENV,
                COACH_HOST_ENV: "0.0.0.0",
                COACH_ALLOW_EXTERNAL_ENV: "1",
                COACH_TOKEN_ENV: "my-token",
            }
        )

        self.assertEqual("0.0.0.0", config.coach_host)
        self.assertTrue(config.coach_allow_external)
        self.assertEqual("my-token", config.coach_token)


if __name__ == "__main__":
    unittest.main()
