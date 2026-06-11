"""Ordered, transactional SQLite schema migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files

MIGRATION_PACKAGE = "trainingos.storage.sql"


class MigrationError(RuntimeError):
    """Raised when the durable schema cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str


def discover_migrations() -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    for resource in files(MIGRATION_PACKAGE).iterdir():
        if not resource.name.endswith(".sql"):
            continue
        version_text, separator, name = resource.name[:-4].partition("_")
        if not separator or not version_text.isdigit() or not name:
            raise MigrationError(f"invalid migration filename: {resource.name}")
        sql = resource.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(version_text),
                name=name,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError("migration versions must be contiguous and start at 1")
    return tuple(migrations)


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] | None = None,
) -> tuple[AppliedMigration, ...]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.commit()

    available = discover_migrations() if migrations is None else migrations
    applied = _load_applied_migrations(connection)
    _validate_applied_migrations(applied, available)

    for migration in available[len(applied) :]:
        _apply_migration(connection, migration)

    return _load_applied_migrations(connection)


def _load_applied_migrations(
    connection: sqlite3.Connection,
) -> tuple[AppliedMigration, ...]:
    rows = connection.execute(
        """
        SELECT version, name, checksum, applied_at
        FROM schema_migrations
        ORDER BY version
        """
    ).fetchall()
    return tuple(AppliedMigration(*row) for row in rows)


def _validate_applied_migrations(
    applied: tuple[AppliedMigration, ...],
    available: tuple[Migration, ...],
) -> None:
    if len(applied) > len(available):
        raise MigrationError("database contains migrations unavailable to this build")
    for index, existing in enumerate(applied):
        expected = available[index]
        if (
            existing.version != expected.version
            or existing.name != expected.name
            or existing.checksum != expected.checksum
        ):
            raise MigrationError(
                f"applied migration {existing.version} does not match this build"
            )


def _apply_migration(
    connection: sqlite3.Connection,
    migration: Migration,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _sql_statements(migration.sql):
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                migration.version,
                migration.name,
                migration.checksum,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise MigrationError(
            f"migration {migration.version}_{migration.name} failed"
        ) from error


def _sql_statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer = ""
    for character in sql:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration contains an incomplete SQL statement")
    return tuple(statements)
