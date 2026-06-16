"""Content-addressed raw source artifact retention."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RawArtifact:
    raw_record_id: str
    checksum: str
    storage_path: Path


class RawArtifactStore:
    """Retains source bytes on disk and records their lineage in SQLite."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().absolute()

    def retain_bytes(
        self,
        connection: sqlite3.Connection,
        *,
        sync_run_id: str,
        source: str,
        external_id: str,
        record_kind: str,
        content_type: str,
        content: bytes,
        source_updated_at: datetime | None,
        ingested_at: datetime,
    ) -> RawArtifact:
        digest = hashlib.sha256(content).hexdigest()
        checksum = f"sha256:{digest}"
        storage_path = self._write_content(digest, content)
        raw_record_id = f"raw:{source}:{external_id}:{digest}"
        connection.execute(
            """
            INSERT OR IGNORE INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, storage_path, source_updated_at,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_record_id,
                sync_run_id,
                source,
                external_id,
                record_kind,
                content_type,
                checksum,
                str(storage_path),
                _timestamp(source_updated_at),
                _timestamp(ingested_at),
            ),
        )
        row = connection.execute(
            """
            SELECT raw_record_id, checksum, storage_path
            FROM raw_source_records
            WHERE source = ? AND external_id = ? AND checksum = ?
            """,
            (source, external_id, checksum),
        ).fetchone()
        if row is None:
            raise RuntimeError("raw source record was not retained")
        return RawArtifact(
            raw_record_id=row["raw_record_id"],
            checksum=row["checksum"],
            storage_path=Path(row["storage_path"]),
        )

    def _write_content(self, digest: str, content: bytes) -> Path:
        path = self._root / "sha256" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary_path = path.with_suffix(".tmp")
            temporary_path.write_bytes(content)
            temporary_path.replace(path)
        return path


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("raw artifact timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()
