"""Command line entrypoint for manual FIT imports."""

from __future__ import annotations

import argparse
from pathlib import Path

from trainingos.config import AppConfig
from trainingos.ingestion.fit import ManualFitAdapter, ManualFitHandler
from trainingos.ingestion.raw import RawArtifactStore
from trainingos.ingestion.sync import SyncRunner, SyncStatus
from trainingos.storage import apply_migrations, connect_database


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

    config = AppConfig.from_env()
    connection = connect_database(config.database_path)
    try:
        apply_migrations(connection)
        report = SyncRunner(connection).run(
            ManualFitAdapter(args.paths),
            ManualFitHandler(
                RawArtifactStore(config.raw_data_dir),
                timezone=args.timezone or config.local_timezone,
            ),
        )
    finally:
        connection.close()
    return 0 if report.status is SyncStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
