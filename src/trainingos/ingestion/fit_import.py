"""Command line entrypoint for manual FIT imports."""

from __future__ import annotations

import argparse
from pathlib import Path

from trainingos.config import AppConfig
from trainingos.ingestion.fit import ManualFitAdapter, ManualFitHandler
from trainingos.ingestion.raw import RawArtifactStore
from trainingos.ingestion.sync import SyncError, SyncRunner, SyncStatus
from trainingos.refresh import refresh_training_data
from trainingos.storage import DatabaseConnectionError, apply_migrations, connect_database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import local FIT files")
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="FIT file, directory, or Garmin export zip paths to import",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone for normalized records; defaults to config",
    )
    args = parser.parse_args(argv)

    for path in args.paths:
        if not path.expanduser().exists():
            parser.error(f"import path does not exist: {path}")

    try:
        config = AppConfig.from_env()
        connection = connect_database(config.database_path)
    except (DatabaseConnectionError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")
    try:
        apply_migrations(connection)
        timezone = args.timezone or config.local_timezone
        report = SyncRunner(connection).run(
            ManualFitAdapter(args.paths),
            ManualFitHandler(
                RawArtifactStore(config.raw_data_dir),
                timezone=timezone,
            ),
        )
        if report.status is SyncStatus.COMPLETED:
            try:
                refresh_training_data(connection, timezone=timezone)
            except Exception as error:
                parser.exit(2, f"{parser.prog}: error: refresh failed: {error}\n")
    except SyncError as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")
    finally:
        connection.close()
    return 0 if report.status is SyncStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
