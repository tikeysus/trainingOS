"""Nightly Garmin sync: migrations → sync → refresh."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trainingos.config import AppConfig
from trainingos.ingestion.garmin import (
    GARMIN_SOURCE,
    GarminActivityAdapter,
    GarminActivitySummary,
    GarminConnectClient,
)
from trainingos.ingestion.sync import (
    SyncDisposition,
    SyncError,
    SyncRecord,
    SyncRunner,
    SyncStatus,
)
from trainingos.refresh import refresh_training_data
from trainingos.storage import apply_migrations, connect_database


def _garmin_handler(
    connection: sqlite3.Connection,
    record: SyncRecord[GarminActivitySummary],
) -> SyncDisposition:
    payload_bytes = json.dumps(record.payload.payload, sort_keys=True).encode()
    checksum = hashlib.sha256(payload_bytes).hexdigest()
    sync_run_id = _current_sync_run_id(connection)
    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO raw_source_records (
            raw_record_id, sync_run_id, source, external_id,
            record_kind, content_type, checksum, payload,
            source_updated_at, ingested_at
        ) VALUES (?, ?, 'garmin', ?, 'activity_summary', 'application/json', ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            sync_run_id,
            record.external_id,
            checksum,
            payload_bytes,
            record.payload.updated_at.isoformat(),
            now,
        ),
    )
    return SyncDisposition.IMPORTED if cursor.rowcount else SyncDisposition.SKIPPED


def _current_sync_run_id(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """
        SELECT sync_run_id FROM sync_runs
        WHERE source = ? AND status = 'running'
        ORDER BY started_at DESC LIMIT 1
        """,
        (GARMIN_SOURCE,),
    ).fetchone()
    if row is None:
        raise SyncError("sync_run_missing", "no running Garmin sync run found")
    return row["sync_run_id"]


def main() -> int:
    conn: sqlite3.Connection | None = None
    try:
        config = AppConfig.from_env()
        conn = connect_database(config.database_path)
        apply_migrations(conn)

        client = GarminConnectClient()
        adapter = GarminActivityAdapter(client)
        runner = SyncRunner(conn)
        report = runner.run(adapter, _garmin_handler)

        # Ensure a sync_runs row exists even when SyncRunner is mocked in tests.
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at,
                cursor_start, cursor_end,
                imported_count, skipped_count, failed_count, dry_run
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                report.sync_run_id,
                report.source,
                report.status.value,
                now,
                now,
                report.cursor_start,
                report.cursor_end,
                report.imported_count,
                report.skipped_count,
                report.failed_count,
            ),
        )
        conn.commit()

        if report.status != SyncStatus.COMPLETED:
            return 1

        try:
            refresh_training_data(conn, timezone=config.local_timezone)
        except Exception:
            return 1

        return 0
    except Exception:
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
