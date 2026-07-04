"""Local evidence document generation and retrieval."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

DOCUMENT_VERSION = "1.0.0"

DOCUMENT_TYPES = frozenset(
    {
        "activity",
        "workout",
        "week",
        "note",
        "race",
        "training_block",
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    document_id: str
    document_type: str
    source_record_id: str
    source_updated_at: str
    title: str
    body: str
    metadata: dict[str, object]
    evidence_record_ids: tuple[str, ...]
    caveats: tuple[str, ...]
    document_version: str = DOCUMENT_VERSION
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        if self.document_type not in DOCUMENT_TYPES:
            raise ValueError(f"unsupported document type: {self.document_type}")
        _require_text(self.document_id, "document_id")
        _require_text(self.source_record_id, "source_record_id")
        _require_text(self.source_updated_at, "source_updated_at")
        _require_text(self.title, "title")
        _require_text(self.body, "body")
        for record_id in self.evidence_record_ids:
            _require_text(record_id, "evidence_record_id")
        for caveat in self.caveats:
            _require_text(caveat, "caveat")


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    document: RetrievalDocument
    rank: float


@dataclass(frozen=True, slots=True)
class GenerationReport:
    generated_count: int
    stale_count: int
    deleted_count: int


def generate_retrieval_documents(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> GenerationReport:
    """Generate compact local evidence documents from persisted SQLite facts."""
    generated_at = _timestamp(now or datetime.now(UTC))
    documents = tuple(_generate_documents(connection))
    expected_ids = {document.document_id for document in documents}
    existing_ids = {
        row["document_id"]
        for row in connection.execute(
            "SELECT document_id FROM retrieval_documents"
        ).fetchall()
    }
    for document in documents:
        _upsert_document(connection, document, generated_at)
    deleted_ids = sorted(existing_ids - expected_ids)
    for document_id in deleted_ids:
        _delete_document(connection, document_id)
    return GenerationReport(
        generated_count=len(documents),
        stale_count=sum(1 for document in documents if document.stale_reason is not None),
        deleted_count=len(deleted_ids),
    )


def search_retrieval_documents(
    connection: sqlite3.Connection,
    query: str,
    *,
    document_types: tuple[str, ...] | None = None,
    limit: int = 20,
) -> tuple[RetrievalResult, ...]:
    """Search local retrieval documents using SQLite FTS5."""
    _require_text(query, "query")
    fts_query = _fts_query(query)
    if limit <= 0:
        raise ValueError("limit must be positive")
    if document_types is not None:
        for document_type in document_types:
            if document_type not in DOCUMENT_TYPES:
                raise ValueError(f"unsupported document type: {document_type}")
    filters = ["document.stale_reason IS NULL"]
    parameters: list[object] = [fts_query]
    if document_types:
        placeholders = ",".join("?" for _ in document_types)
        filters.append(f"document.document_type IN ({placeholders})")
        parameters.extend(document_types)
    parameters.append(limit)
    rows = connection.execute(
        f"""
        SELECT document.*, bm25(retrieval_document_fts) AS rank
        FROM retrieval_document_fts AS fts
        JOIN retrieval_documents AS document
          ON document.document_id = fts.document_id
        WHERE retrieval_document_fts MATCH ?
          AND {" AND ".join(filters)}
        ORDER BY rank, document.generated_at DESC, document.document_id
        LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    return tuple(
        RetrievalResult(document=_document_from_row(row), rank=float(row["rank"]))
        for row in rows
    )


def _generate_documents(
    connection: sqlite3.Connection,
) -> Iterable[RetrievalDocument]:
    yield from _activity_documents(connection)
    yield from _workout_documents(connection)
    yield from _week_documents(connection)
    yield from _note_documents(connection)
    yield from _race_documents(connection)
    yield from _training_block_documents(connection)


def _activity_documents(connection: sqlite3.Connection) -> Iterable[RetrievalDocument]:
    rows = connection.execute(
        """
        SELECT record.record_id, record.updated_at, record.timezone,
               activity.activity_type, activity.started_at,
               activity.duration_seconds, activity.distance_metres, activity.title
        FROM activities AS activity
        JOIN records AS record ON record.record_id = activity.record_id
        ORDER BY activity.started_at, record.record_id
        """
    ).fetchall()
    for row in rows:
        metrics = _metric_values(connection, row["record_id"])
        weather = _activity_weather(connection, row["record_id"])
        notes = _linked_notes(connection, row["record_id"])
        caveats = list(_caveats(connection, row["record_id"]))
        if row["distance_metres"] is None:
            caveats.append("activity distance is missing")
        if not weather:
            caveats.append("weather observation is missing")
        lines = [
            f"Activity {row['record_id']} is a {row['activity_type']} on {row['started_at']} ({row['timezone']}).",
            f"Duration: {_format_seconds(row['duration_seconds'])}.",
        ]
        if row["distance_metres"] is not None:
            lines.append(f"Distance: {_format_metres(row['distance_metres'])}.")
        if row["title"]:
            lines.append(f"Title: {row['title']}.")
        if weather:
            lines.append("Weather: " + "; ".join(weather) + ".")
        if metrics:
            lines.append("Measured metrics: " + "; ".join(metrics) + ".")
        if notes:
            lines.append("Linked notes: " + "; ".join(notes) + ".")
        if caveats:
            lines.append("Data gaps/caveats: " + "; ".join(caveats) + ".")
        yield _document(
            "activity",
            row["record_id"],
            _source_updated_at(connection, row["record_id"]),
            f"Activity {row['started_at']} {row['activity_type']}",
            lines,
            metadata={
                "activity_type": row["activity_type"],
                "started_at": row["started_at"],
                "timezone": row["timezone"],
            },
            evidence=(
                row["record_id"],
                *_weather_ids(connection, row["record_id"]),
                *_linked_note_ids(connection, row["record_id"]),
            ),
            caveats=tuple(dict.fromkeys(caveats)),
        )


def _workout_documents(connection: sqlite3.Connection) -> Iterable[RetrievalDocument]:
    rows = connection.execute(
        """
        SELECT record.record_id, record.updated_at, record.timezone,
               activity.started_at, activity.duration_seconds,
               activity.distance_metres, activity.title
        FROM activities AS activity
        JOIN records AS record ON record.record_id = activity.record_id
        WHERE activity.activity_type = 'run'
        ORDER BY activity.started_at, record.record_id
        """
    ).fetchall()
    for row in rows:
        laps = _laps(connection, row["record_id"])
        metrics = _activity_metric_evidence(connection, row["record_id"])
        caveats = list(_caveats(connection, row["record_id"]))
        if row["distance_metres"] is None:
            caveats.append("workout distance is missing")
        lines = [
            f"Workout {row['record_id']} is a run on {row['started_at']} ({row['timezone']}).",
            f"Duration: {_format_seconds(row['duration_seconds'])}.",
        ]
        if row["distance_metres"] is not None:
            lines.append(f"Distance: {_format_metres(row['distance_metres'])}.")
        if row["title"]:
            lines.append(f"Title: {row['title']}.")
        if laps:
            lines.append("Laps: " + "; ".join(laps) + ".")
        else:
            caveats.append("lap details are missing")
        if metrics:
            lines.append("Computed workout evidence: " + "; ".join(metrics) + ".")
        if caveats:
            lines.append("Data gaps/caveats: " + "; ".join(caveats) + ".")
        yield _document(
            "workout",
            row["record_id"],
            _source_updated_at(connection, row["record_id"]),
            f"Workout {row['started_at']}",
            lines,
            metadata={"started_at": row["started_at"], "timezone": row["timezone"]},
            evidence=(row["record_id"], *_lap_ids(connection, row["record_id"])),
            caveats=tuple(dict.fromkeys(caveats)),
        )


def _week_documents(connection: sqlite3.Connection) -> Iterable[RetrievalDocument]:
    rows = connection.execute(
        """
        SELECT record.record_id, record.updated_at, record.timezone,
               summary.week_start_date, summary.week_end_date, summary.run_count,
               summary.distance_metres, summary.duration_seconds,
               summary.long_run_metres,
               summary.average_pace_seconds_per_kilometre,
               summary.data_gaps_json
        FROM weekly_summaries AS summary
        JOIN records AS record ON record.record_id = summary.record_id
        ORDER BY summary.week_start_date, record.record_id
        """
    ).fetchall()
    for row in rows:
        metrics = _metrics_in_window(
            connection,
            row["week_start_date"],
            row["week_end_date"],
        )
        caveats = list(_caveats(connection, row["record_id"]))
        caveats.extend(json.loads(row["data_gaps_json"]))
        caveats.extend(
            _metric_caveats_in_window(
                connection,
                row["week_start_date"],
                row["week_end_date"],
            )
        )
        lines = [
            f"Week {row['week_start_date']} to {row['week_end_date']} ({row['timezone']}).",
            f"Runs: {row['run_count']}.",
            f"Distance: {_format_metres(row['distance_metres'])}.",
            f"Duration: {_format_seconds(row['duration_seconds'])}.",
        ]
        if row["long_run_metres"] is not None:
            lines.append(f"Long run: {_format_metres(row['long_run_metres'])}.")
        if row["average_pace_seconds_per_kilometre"] is not None:
            lines.append(
                "Average pace: "
                f"{float(row['average_pace_seconds_per_kilometre']):.1f} s/km."
            )
        if metrics:
            lines.append("Computed metrics: " + "; ".join(metrics) + ".")
        if caveats:
            lines.append("Data gaps/caveats: " + "; ".join(caveats) + ".")
        yield _document(
            "week",
            row["record_id"],
            _week_source_updated_at(
                connection,
                row["record_id"],
                row["week_start_date"],
                row["week_end_date"],
            ),
            f"Week {row['week_start_date']}",
            lines,
            metadata={
                "week_start_date": row["week_start_date"],
                "week_end_date": row["week_end_date"],
                "timezone": row["timezone"],
            },
            evidence=(row["record_id"], *_evidence_ids(connection, row["record_id"])),
            caveats=tuple(dict.fromkeys(caveats)),
        )


def _note_documents(connection: sqlite3.Connection) -> Iterable[RetrievalDocument]:
    rows = connection.execute(
        """
        SELECT record.record_id, record.updated_at, record.timezone,
               note.occurred_at, note.note_kind, note.note_text
        FROM context_notes AS note
        JOIN records AS record ON record.record_id = note.record_id
        ORDER BY note.occurred_at, record.record_id
        """
    ).fetchall()
    for row in rows:
        links = _note_links(connection, row["record_id"])
        lines = [
            f"Context note {row['record_id']} is a {row['note_kind']} note from {row['occurred_at']} ({row['timezone']}).",
            f"Note text: {row['note_text']}.",
            "Provenance: user entered.",
        ]
        if links:
            lines.append("Linked records: " + "; ".join(links) + ".")
        yield _document(
            "note",
            row["record_id"],
            row["updated_at"],
            f"{row['note_kind']} note {row['occurred_at']}",
            lines,
            metadata={
                "occurred_at": row["occurred_at"],
                "note_kind": row["note_kind"],
                "timezone": row["timezone"],
            },
            evidence=(row["record_id"], *links),
            caveats=(),
        )


def _race_documents(connection: sqlite3.Connection) -> Iterable[RetrievalDocument]:
    rows = connection.execute(
        """
        SELECT record.record_id, record.updated_at, record.timezone,
               race.name, race.started_at, race.distance_metres,
               race.result_duration_seconds, race.target_duration_seconds
        FROM races AS race
        JOIN records AS record ON record.record_id = race.record_id
        ORDER BY race.started_at, record.record_id
        """
    ).fetchall()
    for row in rows:
        caveats = list(_caveats(connection, row["record_id"]))
        if row["result_duration_seconds"] is None:
            caveats.append("race result is missing")
        if row["target_duration_seconds"] is None:
            caveats.append("race target is missing")
        lines = [
            f"Race {row['name']} starts at {row['started_at']} ({row['timezone']}).",
            f"Distance: {_format_metres(row['distance_metres'])}.",
        ]
        if row["target_duration_seconds"] is not None:
            lines.append(f"Target duration: {_format_seconds(row['target_duration_seconds'])}.")
        if row["result_duration_seconds"] is not None:
            lines.append(f"Result duration: {_format_seconds(row['result_duration_seconds'])}.")
        if caveats:
            lines.append("Data gaps/caveats: " + "; ".join(caveats) + ".")
        yield _document(
            "race",
            row["record_id"],
            row["updated_at"],
            f"Race {row['name']}",
            lines,
            metadata={"started_at": row["started_at"], "timezone": row["timezone"]},
            evidence=(row["record_id"],),
            caveats=tuple(dict.fromkeys(caveats)),
        )


def _training_block_documents(
    connection: sqlite3.Connection,
) -> Iterable[RetrievalDocument]:
    rows = connection.execute(
        """
        SELECT record.record_id, record.updated_at, record.timezone,
               block.goal, block.start_date, block.race_date, block.ended_at
        FROM training_blocks AS block
        JOIN records AS record ON record.record_id = block.record_id
        ORDER BY block.start_date, record.record_id
        """
    ).fetchall()
    for row in rows:
        end_date = row["race_date"] if row["race_date"] else (row["ended_at"][:10] if row["ended_at"] else None)
        weeks = _block_weeks(connection, row["start_date"], end_date) if end_date else []
        caveats = list(_caveats(connection, row["record_id"]))
        if not weeks:
            caveats.append("no weekly summaries are available for this block")
        if not row["race_date"]:
            caveats.append("race date is missing")
        end_date_display = end_date or "(ongoing)"
        lines = [
            f"Training block {row['goal']} runs from {row['start_date']} to {end_date_display} ({row['timezone']}).",
        ]
        if row["race_date"]:
            lines.append(f"Race date: {row['race_date']}.")
        if weeks:
            lines.append("Included weeks: " + "; ".join(weeks) + ".")
        if caveats:
            lines.append("Data gaps/caveats: " + "; ".join(caveats) + ".")
        yield _document(
            "training_block",
            row["record_id"],
            _block_source_updated_at(connection, row["record_id"], row["start_date"], end_date),
            f"Training block {row['goal']}",
            lines,
            metadata={
                "start_date": row["start_date"],
                "race_date": row["race_date"],
                "ended_at": row["ended_at"],
                "timezone": row["timezone"],
            },
            evidence=(row["record_id"], *[week.split(" ", 1)[0] for week in weeks]),
            caveats=tuple(dict.fromkeys(caveats)),
        )


def _document(
    document_type: str,
    source_record_id: str,
    source_updated_at: str,
    title: str,
    lines: list[str],
    *,
    metadata: dict[str, object],
    evidence: tuple[str, ...],
    caveats: tuple[str, ...],
    stale_reason: str | None = None,
) -> RetrievalDocument:
    evidence_ids = tuple(dict.fromkeys(record_id for record_id in evidence if record_id))
    return RetrievalDocument(
        document_id=f"retrieval_document:{document_type}:{source_record_id}",
        document_type=document_type,
        source_record_id=source_record_id,
        source_updated_at=source_updated_at,
        title=title,
        body="\n".join(lines),
        metadata=metadata,
        evidence_record_ids=evidence_ids,
        caveats=caveats,
        stale_reason=stale_reason,
    )


def _upsert_document(
    connection: sqlite3.Connection,
    document: RetrievalDocument,
    generated_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO retrieval_documents (
            document_id, document_type, source_record_id, source_updated_at,
            title, body, metadata_json, evidence_json, caveats_json,
            document_version, generated_at, stale_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (document_id) DO UPDATE SET
            document_type = excluded.document_type,
            source_record_id = excluded.source_record_id,
            source_updated_at = excluded.source_updated_at,
            title = excluded.title,
            body = excluded.body,
            metadata_json = excluded.metadata_json,
            evidence_json = excluded.evidence_json,
            caveats_json = excluded.caveats_json,
            document_version = excluded.document_version,
            generated_at = excluded.generated_at,
            stale_reason = excluded.stale_reason
        """,
        (
            document.document_id,
            document.document_type,
            document.source_record_id,
            document.source_updated_at,
            document.title,
            document.body,
            json.dumps(document.metadata, sort_keys=True),
            json.dumps(list(document.evidence_record_ids), sort_keys=True),
            json.dumps(list(document.caveats), sort_keys=True),
            document.document_version,
            generated_at,
            document.stale_reason,
        ),
    )
    connection.execute(
        "DELETE FROM retrieval_document_fts WHERE document_id = ?",
        (document.document_id,),
    )
    if document.stale_reason is None:
        connection.execute(
            """
            INSERT INTO retrieval_document_fts (document_id, title, body)
            VALUES (?, ?, ?)
            """,
            (document.document_id, document.title, document.body),
        )


def _delete_document(connection: sqlite3.Connection, document_id: str) -> None:
    connection.execute(
        "DELETE FROM retrieval_document_fts WHERE document_id = ?",
        (document_id,),
    )
    connection.execute(
        "DELETE FROM retrieval_documents WHERE document_id = ?",
        (document_id,),
    )


def _document_from_row(row: sqlite3.Row) -> RetrievalDocument:
    return RetrievalDocument(
        document_id=row["document_id"],
        document_type=row["document_type"],
        source_record_id=row["source_record_id"],
        source_updated_at=row["source_updated_at"],
        title=row["title"],
        body=row["body"],
        metadata=json.loads(row["metadata_json"]),
        evidence_record_ids=tuple(json.loads(row["evidence_json"])),
        caveats=tuple(json.loads(row["caveats_json"])),
        document_version=row["document_version"],
        stale_reason=row["stale_reason"],
    )


def _metric_values(connection: sqlite3.Connection, owner_record_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT metric_key, metric_status, metric_value, unit, quality
        FROM metric_values
        WHERE owner_record_id = ?
        ORDER BY metric_key
        """,
        (owner_record_id,),
    ).fetchall()
    values = []
    for row in rows:
        if row["metric_status"] == "measured":
            values.append(f"{row['metric_key']}={row['metric_value']} {row['unit']}")
        else:
            values.append(f"{row['metric_key']}={row['metric_status']}")
    return tuple(values)


def _activity_weather(connection: sqlite3.Connection, activity_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT weather.record_id, weather.observed_at, weather.condition
        FROM activity_weather_observations AS link
        JOIN weather_observations AS weather
          ON weather.record_id = link.weather_observation_id
        WHERE link.activity_id = ?
        ORDER BY weather.observed_at, weather.record_id
        """,
        (activity_id,),
    ).fetchall()
    values = []
    for row in rows:
        metrics = _metric_values(connection, row["record_id"])
        parts = [row["observed_at"]]
        if row["condition"]:
            parts.append(row["condition"])
        parts.extend(metrics)
        values.append(" ".join(parts))
    return tuple(values)


def _weather_ids(connection: sqlite3.Connection, activity_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT weather_observation_id
        FROM activity_weather_observations
        WHERE activity_id = ?
        ORDER BY weather_observation_id
        """,
        (activity_id,),
    ).fetchall()
    return tuple(row["weather_observation_id"] for row in rows)


def _linked_notes(connection: sqlite3.Connection, record_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT note.note_kind, note.note_text
        FROM context_note_links AS link
        JOIN context_notes AS note ON note.record_id = link.note_id
        WHERE link.linked_record_id = ?
        ORDER BY note.occurred_at, note.record_id
        """,
        (record_id,),
    ).fetchall()
    return tuple(f"{row['note_kind']} note: {row['note_text']}" for row in rows)


def _linked_note_ids(connection: sqlite3.Connection, record_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT note_id
        FROM context_note_links
        WHERE linked_record_id = ?
        ORDER BY note_id
        """,
        (record_id,),
    ).fetchall()
    return tuple(row["note_id"] for row in rows)


def _note_links(connection: sqlite3.Connection, note_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT linked_record_id
        FROM context_note_links
        WHERE note_id = ?
        ORDER BY linked_record_id
        """,
        (note_id,),
    ).fetchall()
    return tuple(row["linked_record_id"] for row in rows)


def _laps(connection: sqlite3.Connection, activity_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT lap_index, duration_seconds, distance_metres
        FROM laps
        WHERE activity_id = ?
        ORDER BY lap_index
        """,
        (activity_id,),
    ).fetchall()
    values = []
    for row in rows:
        parts = [
            f"lap {row['lap_index']}",
            _format_seconds(row["duration_seconds"]),
        ]
        if row["distance_metres"] is not None:
            parts.append(_format_metres(row["distance_metres"]))
        values.append(" ".join(parts))
    return tuple(values)


def _lap_ids(connection: sqlite3.Connection, activity_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT record_id
        FROM laps
        WHERE activity_id = ?
        ORDER BY lap_index
        """,
        (activity_id,),
    ).fetchall()
    return tuple(row["record_id"] for row in rows)


def _activity_metric_evidence(
    connection: sqlite3.Connection,
    activity_id: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT metric.metric_key, metric.metric_value, metric.unit,
               metric.quality, metric.summary, record.method_name,
               record.method_version
        FROM metric_evidence AS metric
        JOIN records AS record ON record.record_id = metric.record_id
        JOIN provenance_evidence AS evidence ON evidence.record_id = metric.record_id
        WHERE evidence.evidence_record_id = ?
        ORDER BY metric.metric_key, metric.window_start, metric.record_id
        """,
        (activity_id,),
    ).fetchall()
    values = []
    for row in rows:
        method = f"{row['method_name']}:{row['method_version']}"
        values.append(
            f"{row['metric_key']}={row['metric_value']} {row['unit']} via {method}"
        )
    return tuple(values)


def _metrics_in_window(
    connection: sqlite3.Connection,
    week_start_date: str,
    week_end_date: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT metric.metric_key, metric.metric_value, metric.unit,
               metric.quality, metric.summary, record.method_name,
               record.method_version
        FROM metric_evidence AS metric
        JOIN records AS record ON record.record_id = metric.record_id
        WHERE date(metric.window_start) <= date(?)
          AND date(metric.window_end, '-1 day') >= date(?)
        ORDER BY metric.metric_key, metric.record_id
        """,
        (week_end_date, week_start_date),
    ).fetchall()
    values = []
    for row in rows:
        method = f"{row['method_name']}:{row['method_version']}"
        quality = "" if row["quality"] is None else f", quality {row['quality']}"
        values.append(
            f"{row['metric_key']}={row['metric_value']} {row['unit']} via {method}{quality}"
        )
    return tuple(values)


def _metric_caveats_in_window(
    connection: sqlite3.Connection,
    week_start_date: str,
    week_end_date: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT DISTINCT caveat.caveat
        FROM metric_evidence AS metric
        JOIN provenance_caveats AS caveat ON caveat.record_id = metric.record_id
        WHERE date(metric.window_start) <= date(?)
          AND date(metric.window_end, '-1 day') >= date(?)
        ORDER BY caveat.caveat
        """,
        (week_end_date, week_start_date),
    ).fetchall()
    return tuple(row["caveat"] for row in rows)


def _block_weeks(
    connection: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT record_id, week_start_date, week_end_date, run_count,
               distance_metres, data_gaps_json
        FROM weekly_summaries
        WHERE week_start_date >= ?
          AND week_start_date <= ?
        ORDER BY week_start_date
        """,
        (start_date, end_date),
    ).fetchall()
    return tuple(
        f"{row['record_id']} {row['week_start_date']} to {row['week_end_date']}: "
        f"{row['run_count']} runs, {_format_metres(row['distance_metres'])}"
        for row in rows
    )


def _block_source_updated_at(
    connection: sqlite3.Connection,
    block_id: str,
    start_date: str,
    end_date: str,
) -> str:
    row = connection.execute(
        """
        SELECT MAX(updated_at) AS updated_at
        FROM records
        WHERE record_id = ?
           OR record_id IN (
                SELECT record_id
                FROM weekly_summaries
                WHERE week_start_date >= ?
                  AND week_start_date <= ?
           )
        """,
        (block_id, start_date, end_date),
    ).fetchone()
    return row["updated_at"]


def _week_source_updated_at(
    connection: sqlite3.Connection,
    week_record_id: str,
    week_start_date: str,
    week_end_date: str,
) -> str:
    row = connection.execute(
        """
        SELECT MAX(updated_at) AS updated_at
        FROM records
        WHERE record_id = ?
           OR record_id IN (
                SELECT record_id
                FROM metric_evidence
                WHERE date(window_start) <= date(?)
                  AND date(window_end, '-1 day') >= date(?)
           )
        """,
        (week_record_id, week_end_date, week_start_date),
    ).fetchone()
    return row["updated_at"]


def _caveats(connection: sqlite3.Connection, record_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT caveat
        FROM provenance_caveats
        WHERE record_id = ?
        ORDER BY position
        """,
        (record_id,),
    ).fetchall()
    return tuple(row["caveat"] for row in rows)


def _evidence_ids(connection: sqlite3.Connection, record_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT evidence_record_id
        FROM provenance_evidence
        WHERE record_id = ?
        ORDER BY position
        """,
        (record_id,),
    ).fetchall()
    return tuple(row["evidence_record_id"] for row in rows)


def _source_updated_at(connection: sqlite3.Connection, source_record_id: str) -> str:
    row = connection.execute(
        """
        SELECT MAX(updated_at) AS updated_at
        FROM records
        WHERE record_id = ?
           OR record_id IN (
                SELECT record_id
                FROM metric_evidence
                WHERE record_id IN (
                    SELECT record_id
                    FROM provenance_evidence
                    WHERE evidence_record_id = ?
                )
           )
           OR record_id IN (
                SELECT weather_observation_id
                FROM activity_weather_observations
                WHERE activity_id = ?
           )
           OR record_id IN (
                SELECT note_id
                FROM context_note_links
                WHERE linked_record_id = ?
           )
           OR record_id IN (
                SELECT record_id
                FROM laps
                WHERE activity_id = ?
           )
        """,
        (
            source_record_id,
            source_record_id,
            source_record_id,
            source_record_id,
            source_record_id,
        ),
    ).fetchone()
    return row["updated_at"]


def _format_metres(value: object) -> str:
    return f"{float(value):.0f} m"


def _format_seconds(value: object) -> str:
    return f"{float(value):.0f} s"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w]+", query)
    if not tokens:
        raise ValueError("query must contain searchable text")
    return " ".join(f'"{token}"' for token in tokens)


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


__all__ = [
    "DOCUMENT_VERSION",
    "GenerationReport",
    "RetrievalDocument",
    "RetrievalResult",
    "generate_retrieval_documents",
    "search_retrieval_documents",
]
