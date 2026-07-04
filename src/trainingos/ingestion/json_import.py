"""CLI entry point: python -m trainingos.ingestion.json_import <path>"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: json_import <export-path>", file=sys.stderr)
        sys.exit(2)

    import_path = Path(sys.argv[1])

    if not import_path.exists():
        print(f"import path does not exist: {import_path}", file=sys.stderr)
        sys.exit(2)

    from trainingos.ingestion.garmin_json import GarminJsonHealthAdapter, GarminJsonHealthHandler
    from trainingos.ingestion.raw import RawArtifactStore
    from trainingos.ingestion.sync import SyncRunner
    from trainingos.storage import apply_migrations, connect_database

    try:
        adapter = GarminJsonHealthAdapter((import_path,))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    db_path = Path(os.environ.get("TRAININGOS_DB", "var/training.sqlite3"))
    connection = connect_database(db_path)
    apply_migrations(connection)

    raw_dir = db_path.parent / "raw"
    timezone = os.environ.get("TRAININGOS_TIMEZONE", "UTC")

    handler = GarminJsonHealthHandler(RawArtifactStore(raw_dir), timezone=timezone)
    report = SyncRunner(connection).run(adapter, handler)
    print(
        f"status={report.status} imported={report.imported_count} "
        f"skipped={report.skipped_count} failed={report.failed_count}"
    )
    if report.failed_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
