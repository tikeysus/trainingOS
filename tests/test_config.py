from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from trainingos.config import (
    AI_PROVIDER_ENV,
    AI_TIMEOUT_SECONDS_ENV,
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_CHAT_MODEL_ENV,
    AppConfig,
    COACH_HOST_ENV,
    COACH_PORT_ENV,
    DATABASE_PATH_ENV,
    DEFAULT_ANTHROPIC_CHAT_MODEL,
    LOCAL_TIMEZONE_ENV,
    OLLAMA_BASE_URL_ENV,
    OLLAMA_CHAT_MODEL_ENV,
    OLLAMA_EMBEDDING_MODEL_ENV,
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
        self.assertEqual("ollama", config.ai_provider)
        self.assertEqual("http://localhost:11434", config.ollama_base_url)
        self.assertEqual("127.0.0.1", config.coach_host)
        self.assertEqual(8765, config.coach_port)

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

    def test_database_path_rejects_documentation_placeholders(self) -> None:
        with self.assertRaisesRegex(ValueError, "documentation placeholder"):
            AppConfig.from_env(
                {DATABASE_PATH_ENV: "/absolute/path/to/trainingos.sqlite3"}
            )

        with self.assertRaisesRegex(ValueError, "documentation placeholder"):
            AppConfig.from_env({DATABASE_PATH_ENV: "/path/to/trainingos.sqlite3"})

    def test_raw_data_dir_and_timezone_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env(
            {
                RAW_DATA_DIR_ENV: "var/raw",
                LOCAL_TIMEZONE_ENV: "America/Toronto",
            }
        )

        self.assertEqual(Path("var/raw").absolute(), config.raw_data_dir)
        self.assertEqual("America/Toronto", config.local_timezone)

    def test_raw_data_dir_rejects_documentation_placeholder(self) -> None:
        with self.assertRaisesRegex(ValueError, "documentation placeholder"):
            AppConfig.from_env({RAW_DATA_DIR_ENV: "/path/to/raw-artifacts"})

    def test_local_timezone_must_be_valid_iana_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid IANA timezone"):
            AppConfig.from_env({LOCAL_TIMEZONE_ENV: "Toronto"})

    def test_local_ollama_provider_can_be_configured_from_environment(self) -> None:
        config = AppConfig.from_env(
            {
                OLLAMA_BASE_URL_ENV: "http://127.0.0.1:11434/",
                OLLAMA_CHAT_MODEL_ENV: "qwen3:8b",
                OLLAMA_EMBEDDING_MODEL_ENV: "nomic-embed-text",
                AI_TIMEOUT_SECONDS_ENV: "12.5",
                COACH_HOST_ENV: "localhost",
                COACH_PORT_ENV: "8766",
            }
        )

        self.assertEqual("ollama", config.ai_provider)
        self.assertEqual("http://127.0.0.1:11434", config.ollama_base_url)
        self.assertEqual("qwen3:8b", config.ollama_chat_model)
        self.assertEqual("nomic-embed-text", config.ollama_embedding_model)
        self.assertEqual(12.5, config.ai_timeout_seconds)
        self.assertEqual("localhost", config.coach_host)
        self.assertEqual(8766, config.coach_port)

    def test_unknown_ai_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "TRAININGOS_AI_PROVIDER"):
            AppConfig.from_env({AI_PROVIDER_ENV: "openai"})

    def test_ollama_base_url_must_be_local_http_without_credentials(self) -> None:
        invalid_values = (
            "https://localhost:11434",
            "http://example.com:11434",
            "http://user:pass@localhost:11434",
            "http://localhost:11434?token=secret",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, OLLAMA_BASE_URL_ENV):
                    AppConfig.from_env({OLLAMA_BASE_URL_ENV: value})

    def test_ai_model_names_and_timeout_must_be_valid(self) -> None:
        with self.assertRaisesRegex(ValueError, OLLAMA_CHAT_MODEL_ENV):
            AppConfig.from_env({OLLAMA_CHAT_MODEL_ENV: " "})
        with self.assertRaisesRegex(ValueError, OLLAMA_EMBEDDING_MODEL_ENV):
            AppConfig.from_env({OLLAMA_EMBEDDING_MODEL_ENV: ""})
        with self.assertRaisesRegex(ValueError, "positive number"):
            AppConfig.from_env({AI_TIMEOUT_SECONDS_ENV: "0"})

    def test_coach_port_must_be_valid_tcp_port(self) -> None:
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, COACH_PORT_ENV):
                    AppConfig.from_env({COACH_PORT_ENV: value})


    def test_anthropic_provider_is_valid(self) -> None:
        config = AppConfig.from_env(
            {
                AI_PROVIDER_ENV: "anthropic",
                ANTHROPIC_API_KEY_ENV: "sk-ant-test",
                ANTHROPIC_CHAT_MODEL_ENV: "claude-opus-4-7",
            }
        )

        self.assertEqual("anthropic", config.ai_provider)
        self.assertEqual("sk-ant-test", config.anthropic_api_key)
        self.assertEqual("claude-opus-4-7", config.anthropic_chat_model)

    def test_anthropic_provider_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, ANTHROPIC_API_KEY_ENV):
            AppConfig.from_env({AI_PROVIDER_ENV: "anthropic"})

        with self.assertRaisesRegex(ValueError, ANTHROPIC_API_KEY_ENV):
            AppConfig.from_env({AI_PROVIDER_ENV: "anthropic", ANTHROPIC_API_KEY_ENV: "  "})

    def test_anthropic_provider_uses_default_model(self) -> None:
        config = AppConfig.from_env(
            {AI_PROVIDER_ENV: "anthropic", ANTHROPIC_API_KEY_ENV: "sk-ant-test"}
        )

        self.assertEqual(DEFAULT_ANTHROPIC_CHAT_MODEL, config.anthropic_chat_model)

    def test_anthropic_provider_skips_ollama_url_validation(self) -> None:
        config = AppConfig.from_env(
            {
                AI_PROVIDER_ENV: "anthropic",
                ANTHROPIC_API_KEY_ENV: "sk-ant-test",
                OLLAMA_BASE_URL_ENV: "https://example.com:11434",
            }
        )

        self.assertEqual("anthropic", config.ai_provider)

    def test_anthropic_fields_are_none_for_ollama_provider(self) -> None:
        config = AppConfig.from_env({})

        self.assertEqual("ollama", config.ai_provider)
        self.assertIsNone(config.anthropic_api_key)
        self.assertIsNone(config.anthropic_chat_model)


if __name__ == "__main__":
    unittest.main()
