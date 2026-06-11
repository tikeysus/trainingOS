"""SQLite persistence and schema migration support."""

from .database import connect_database
from .migrations import (
    AppliedMigration,
    Migration,
    MigrationError,
    apply_migrations,
    discover_migrations,
)

__all__ = [
    "AppliedMigration",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "connect_database",
    "discover_migrations",
]
