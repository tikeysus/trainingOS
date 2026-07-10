"""Local TrainingOS application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_CHAT_MODEL_ENV = "CLAUDE_MODEL"
COACH_ALLOW_EXTERNAL_ENV = "TRAININGOS_COACH_ALLOW_EXTERNAL"
COACH_HOST_ENV = "TRAININGOS_COACH_HOST"
COACH_PORT_ENV = "TRAININGOS_COACH_PORT"
COACH_TOKEN_ENV = "TRAININGOS_COACH_TOKEN"
DATABASE_PATH_ENV = "TRAININGOS_DB_PATH"
LOCAL_TIMEZONE_ENV = "TRAININGOS_LOCAL_TIMEZONE"
RAW_DATA_DIR_ENV = "TRAININGOS_RAW_DATA_DIR"
DEFAULT_DATABASE_PATH = Path(".local/share/trainingos/trainingos.sqlite3")
DEFAULT_RAW_DATA_DIR = Path(".local/share/trainingos/raw")
DEFAULT_AI_TIMEOUT_SECONDS = 30.0
DEFAULT_ANTHROPIC_CHAT_MODEL = "claude-opus-4-1"
DEFAULT_COACH_HOST = "127.0.0.1"
DEFAULT_COACH_PORT = 8765
PLACEHOLDER_PATH_PREFIXES = (
    "/absolute/path",
    "/path/to",
)


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    raw_data_dir: Path
    local_timezone: str
    ai_timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS
    anthropic_api_key: str | None = None
    anthropic_chat_model: str | None = None
    coach_host: str = DEFAULT_COACH_HOST
    coach_port: int = DEFAULT_COACH_PORT
    coach_allow_external: bool = False
    coach_token: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        values = environ if env is None else env
        configured_path = values.get(DATABASE_PATH_ENV)
        if configured_path:
            _validate_configured_path(DATABASE_PATH_ENV, configured_path)
        database_path = (
            Path(configured_path)
            if configured_path
            else Path.home() / DEFAULT_DATABASE_PATH
        )
        configured_raw_dir = values.get(RAW_DATA_DIR_ENV)
        if configured_raw_dir:
            _validate_configured_path(RAW_DATA_DIR_ENV, configured_raw_dir)
        raw_data_dir = (
            Path(configured_raw_dir)
            if configured_raw_dir
            else Path.home() / DEFAULT_RAW_DATA_DIR
        )
        local_timezone = values.get(LOCAL_TIMEZONE_ENV, "UTC")
        _validate_timezone(local_timezone)
        raw_key = values.get(ANTHROPIC_API_KEY_ENV, "").strip()
        if not raw_key:
            raise ValueError(
                f"{ANTHROPIC_API_KEY_ENV} is required"
            )
        anthropic_api_key = raw_key
        anthropic_chat_model = _configured_text(
            values,
            ANTHROPIC_CHAT_MODEL_ENV,
            DEFAULT_ANTHROPIC_CHAT_MODEL,
        )
        ai_timeout_seconds = _configured_timeout(
            values,
            "TRAININGOS_AI_TIMEOUT_SECONDS",
            DEFAULT_AI_TIMEOUT_SECONDS,
        )
        coach_host = _configured_text(values, COACH_HOST_ENV, DEFAULT_COACH_HOST)
        coach_port = _configured_port(values, COACH_PORT_ENV, DEFAULT_COACH_PORT)
        coach_allow_external = values.get(COACH_ALLOW_EXTERNAL_ENV, "").strip() in {"1", "true", "yes"}
        if not _is_localhost(coach_host) and not coach_allow_external:
            raise ValueError(
                f"TRAININGOS_COACH_ALLOW_EXTERNAL must be set to '1' when binding to a "
                f"non-localhost address ({coach_host!r})"
            )
        coach_token_raw = values.get(COACH_TOKEN_ENV, "").strip()
        coach_token: str | None = coach_token_raw if coach_token_raw else None
        return cls(
            database_path=database_path.expanduser().absolute(),
            raw_data_dir=raw_data_dir.expanduser().absolute(),
            local_timezone=local_timezone,
            ai_timeout_seconds=ai_timeout_seconds,
            anthropic_api_key=anthropic_api_key,
            anthropic_chat_model=anthropic_chat_model,
            coach_host=coach_host,
            coach_port=coach_port,
            coach_allow_external=coach_allow_external,
            coach_token=coach_token,
        )


def _validate_configured_path(name: str, value: str) -> None:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    if any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in PLACEHOLDER_PATH_PREFIXES
    ):
        raise ValueError(
            f"{name} is set to documentation placeholder {value!r}; "
            "set it to a real local path or unset it to use the default"
        )


def _validate_timezone(value: str) -> None:
    if not value or not value.strip():
        raise ValueError("local timezone must not be blank")
    try:
        zone = ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"local timezone must be a valid IANA timezone: {value}") from error
    if zone.key != value:
        raise ValueError(f"local timezone must use its canonical IANA key: {value}")


def _configured_text(
    values: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    value = values.get(name, default)
    if not value or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()


def _configured_timeout(
    values: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    configured = values.get(name)
    if configured is None:
        return default
    try:
        timeout = float(configured)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number") from error
    if timeout <= 0:
        raise ValueError(f"{name} must be a positive number")
    return timeout


def _configured_port(
    values: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    configured = values.get(name)
    if configured is None:
        return default
    try:
        port = int(configured)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer port") from error
    if port <= 0 or port > 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _is_localhost(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}
