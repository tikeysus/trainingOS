"""SQLite connection configuration."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class DatabaseConnectionError(RuntimeError):
    """Raised when the local SQLite database cannot be opened."""


def connect_database(path: Path) -> sqlite3.Connection:
    database_path = path.expanduser().absolute()
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
    except OSError as error:
        raise DatabaseConnectionError(
            f"cannot open local SQLite database at {database_path}: {error}"
        ) from error
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection
