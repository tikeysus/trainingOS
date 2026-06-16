"""Local TrainingOS application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATABASE_PATH_ENV = "TRAININGOS_DB_PATH"
LOCAL_TIMEZONE_ENV = "TRAININGOS_LOCAL_TIMEZONE"
RAW_DATA_DIR_ENV = "TRAININGOS_RAW_DATA_DIR"
DEFAULT_DATABASE_PATH = Path(".local/share/trainingos/trainingos.sqlite3")
DEFAULT_RAW_DATA_DIR = Path(".local/share/trainingos/raw")


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    raw_data_dir: Path
    local_timezone: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        values = environ if env is None else env
        configured_path = values.get(DATABASE_PATH_ENV)
        database_path = (
            Path(configured_path)
            if configured_path
            else Path.home() / DEFAULT_DATABASE_PATH
        )
        configured_raw_dir = values.get(RAW_DATA_DIR_ENV)
        raw_data_dir = (
            Path(configured_raw_dir)
            if configured_raw_dir
            else Path.home() / DEFAULT_RAW_DATA_DIR
        )
        local_timezone = values.get(LOCAL_TIMEZONE_ENV, "UTC")
        _validate_timezone(local_timezone)
        return cls(
            database_path=database_path.expanduser().absolute(),
            raw_data_dir=raw_data_dir.expanduser().absolute(),
            local_timezone=local_timezone,
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
