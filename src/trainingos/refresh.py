"""Refresh derived metrics and local retrieval documents."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trainingos.analytics import DerivationReport, derive_training_metrics
from trainingos.config import AppConfig
from trainingos.retrieval import GenerationReport, generate_retrieval_documents
from trainingos.storage import DatabaseConnectionError, apply_migrations, connect_database


@dataclass(frozen=True, slots=True)
class RefreshReport:
    metrics: DerivationReport
    retrieval: GenerationReport


def refresh_training_data(
    connection: sqlite3.Connection,
    *,
    timezone: str,
    now: datetime | None = None,
) -> RefreshReport:
    try:
        metrics = derive_training_metrics(connection, timezone=timezone, now=now)
        retrieval = generate_retrieval_documents(connection, now=now)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return RefreshReport(metrics=metrics, retrieval=retrieval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite path; defaults to TRAININGOS_DB_PATH or the local user path",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone for derived weekly windows; defaults to config",
    )
    args = parser.parse_args(argv)

    connection: sqlite3.Connection | None = None
    try:
        config = AppConfig.from_env()
        database_path = args.database or config.database_path
        timezone = args.timezone or config.local_timezone
        connection = connect_database(database_path)
        apply_migrations(connection)
        report = refresh_training_data(connection, timezone=timezone)
    except (DatabaseConnectionError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")
        return 2
    finally:
        if connection is not None:
            connection.close()
    print(
        "TrainingOS data refreshed at "
        f"{database_path.expanduser().absolute()} "
        f"({report.metrics.week_count} weeks, "
        f"{report.metrics.metric_count} metrics, "
        f"{report.retrieval.generated_count} retrieval documents, "
        f"{report.retrieval.stale_count} stale, "
        f"{report.retrieval.deleted_count} deleted)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
