"""Local TrainingOS application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Mapping

DATABASE_PATH_ENV = "TRAININGOS_DB_PATH"
DEFAULT_DATABASE_PATH = Path(".local/share/trainingos/trainingos.sqlite3")


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        values = environ if env is None else env
        configured_path = values.get(DATABASE_PATH_ENV)
        path = (
            Path(configured_path)
            if configured_path
            else Path.home() / DEFAULT_DATABASE_PATH
        )
        return cls(database_path=path.expanduser().absolute())
