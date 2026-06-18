"""SQLite persistence and schema migration support."""

from .database import DatabaseConnectionError, connect_database
from .migrations import (
    AppliedMigration,
    Migration,
    MigrationError,
    apply_migrations,
    discover_migrations,
)

__all__ = [
    "AppliedMigration",
    "DatabaseConnectionError",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "connect_database",
    "discover_migrations",
]
