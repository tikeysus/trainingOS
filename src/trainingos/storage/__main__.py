"""Initialize or update the local TrainingOS database."""

from __future__ import annotations

import argparse
from pathlib import Path

from trainingos.config import AppConfig

from .database import DatabaseConnectionError, connect_database
from .migrations import apply_migrations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite path; defaults to TRAININGOS_DB_PATH or the local user path",
    )
    args = parser.parse_args()

    try:
        path = args.database or AppConfig.from_env().database_path
        with connect_database(path) as connection:
            applied = apply_migrations(connection)
    except (DatabaseConnectionError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")
    print(f"Database ready at {path.expanduser().absolute()} ({len(applied)} migrations)")


if __name__ == "__main__":
    main()
