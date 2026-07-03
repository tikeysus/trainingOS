"""Contracts and derivation for race projections from local evidence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from statistics import mean

from trainingos.domain import MethodVersion
from trainingos.domain.common import require_text

RIEGEL_PROJECTION_METHOD = MethodVersion("riegel", "1.0.0")
TRAINING_LOAD_PROJECTION_METHOD = MethodVersion("training_load_projection", "1.0.0")

_RIEGEL_EXPONENT = 1.06
_RIEGEL_UNCERTAINTY_FACTOR = 0.05
_TRAINING_LOAD_UNCERTAINTY_FACTOR = 0.10
_TRAINING_LOAD_MIN_WEEKS = 4
_DEFAULT_PACE_SECONDS_PER_KM = 360.0  # fallback 6:00/km when no pace evidence
_MARATHON_KM = 42195.0 / 1000.0


class ProjectionStatus(StrEnum):
    ESTIMATED = "estimated"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class RaceProjection:
    method: MethodVersion
    status: ProjectionStatus
    target_race_id: str
    projected_duration_seconds: float | None
    uncertainty_seconds: float | None
    evidence_record_ids: tuple[str, ...]
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.target_race_id, "target_race_id")
        for record_id in self.evidence_record_ids:
            require_text(record_id, "evidence_record_id")
        for caveat in self.caveats:
            require_text(caveat, "caveat")
        if self.status is ProjectionStatus.ESTIMATED:
            if self.projected_duration_seconds is None:
                raise ValueError("estimated projection requires projected duration")
            if self.projected_duration_seconds <= 0:
                raise ValueError("projected duration must be positive")
            if self.uncertainty_seconds is None:
                raise ValueError("estimated projection requires uncertainty")
            if self.uncertainty_seconds < 0:
                raise ValueError("uncertainty must not be negative")
            if not self.evidence_record_ids:
                raise ValueError("estimated projection requires evidence")
        else:
            if self.projected_duration_seconds is not None:
                raise ValueError("insufficient-data projection must not estimate")
            if self.uncertainty_seconds is not None:
                raise ValueError("insufficient-data projection must not estimate")
            if not self.caveats:
                raise ValueError("insufficient-data projection requires caveats")


def derive_race_projections(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    lookback_days: int = 365,
) -> int:
    """Compute and persist race projections for all upcoming target races.

    Returns the total number of projection records written.
    """
    now_utc = _utc(now)
    cutoff = now_utc - timedelta(days=lookback_days)
    target_races = _load_target_races(connection, now_utc)
    count = 0
    for race_id, distance_metres in target_races:
        count += _persist_riegel(connection, race_id, distance_metres, now_utc, cutoff)
        count += _persist_training_load(connection, race_id, now_utc, cutoff)
    return count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_target_races(
    connection: sqlite3.Connection,
    now_utc: datetime,
) -> list[tuple[str, float]]:
    rows = connection.execute(
        """
        SELECT record_id, distance_metres FROM races
        WHERE started_at > ?
          AND result_duration_seconds IS NULL
        """,
        (now_utc.isoformat(),),
    ).fetchall()
    return [(row["record_id"], float(row["distance_metres"])) for row in rows]


def _persist_riegel(
    connection: sqlite3.Connection,
    target_race_id: str,
    target_distance_metres: float,
    now_utc: datetime,
    cutoff: datetime,
) -> int:
    record_id = (
        f"projection:{RIEGEL_PROJECTION_METHOD.name}:"
        f"{RIEGEL_PROJECTION_METHOD.version}:{target_race_id}"
    )
    source = connection.execute(
        """
        SELECT record_id, result_duration_seconds, distance_metres
        FROM races
        WHERE record_id != ?
          AND result_duration_seconds IS NOT NULL
          AND started_at <= ?
          AND started_at >= ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (target_race_id, now_utc.isoformat(), cutoff.isoformat()),
    ).fetchone()

    if source is None:
        _upsert_projection_record(
            connection,
            record_id=record_id,
            method=RIEGEL_PROJECTION_METHOD,
            computed_at=now_utc,
            target_race_id=target_race_id,
            status=ProjectionStatus.INSUFFICIENT_DATA,
            projected_duration_seconds=None,
            uncertainty_seconds=None,
            evidence_ids=(),
            caveats=("no race result within lookback window",),
        )
        return 1

    t1 = float(source["result_duration_seconds"])
    d1 = float(source["distance_metres"])
    projected = t1 * (target_distance_metres / d1) ** _RIEGEL_EXPONENT
    uncertainty = projected * _RIEGEL_UNCERTAINTY_FACTOR

    _upsert_projection_record(
        connection,
        record_id=record_id,
        method=RIEGEL_PROJECTION_METHOD,
        computed_at=now_utc,
        target_race_id=target_race_id,
        status=ProjectionStatus.ESTIMATED,
        projected_duration_seconds=projected,
        uncertainty_seconds=uncertainty,
        evidence_ids=(source["record_id"],),
        caveats=(),
    )
    return 1


def _persist_training_load(
    connection: sqlite3.Connection,
    target_race_id: str,
    now_utc: datetime,
    cutoff: datetime,
) -> int:
    record_id = (
        f"projection:{TRAINING_LOAD_PROJECTION_METHOD.name}:"
        f"{TRAINING_LOAD_PROJECTION_METHOD.version}:{target_race_id}"
    )

    rows = connection.execute(
        """
        SELECT me.record_id, me.metric_key, me.metric_value,
               me.window_start, me.window_end
        FROM metric_evidence me
        WHERE me.metric_key IN (
                'weekly_distance', 'weekly_long_run', 'weekly_average_pace'
              )
          AND me.window_start >= ?
          AND me.window_start < ?
        ORDER BY me.window_start DESC
        """,
        (cutoff.isoformat(), now_utc.isoformat()),
    ).fetchall()

    # Group by (window_start, window_end) → week
    windows: dict[tuple[str, str], dict[str, list]] = {}
    for row in rows:
        key = (row["window_start"], row["window_end"])
        windows.setdefault(key, {"distance": [], "long_run": [], "pace": [], "ids": []})
        windows[key]["ids"].append(row["record_id"])
        if row["metric_key"] == "weekly_distance":
            windows[key]["distance"].append(float(row["metric_value"]))
        elif row["metric_key"] == "weekly_long_run":
            windows[key]["long_run"].append(float(row["metric_value"]))
        elif row["metric_key"] == "weekly_average_pace":
            windows[key]["pace"].append(float(row["metric_value"]))

    qualifying_windows = [
        w for w in windows.values()
        if any(v > 0 for v in w["distance"])
    ]
    partial_windows = [
        w for w in windows.values()
        if not any(v > 0 for v in w["distance"])
        and (w["long_run"] or w["pace"])
    ]

    all_evidence_ids = [rid for w in windows.values() for rid in w["ids"]]

    if len(qualifying_windows) < _TRAINING_LOAD_MIN_WEEKS:
        _upsert_projection_record(
            connection,
            record_id=record_id,
            method=TRAINING_LOAD_PROJECTION_METHOD,
            computed_at=now_utc,
            target_race_id=target_race_id,
            status=ProjectionStatus.INSUFFICIENT_DATA,
            projected_duration_seconds=None,
            uncertainty_seconds=None,
            evidence_ids=tuple(all_evidence_ids),
            caveats=(
                f"insufficient training data: {len(qualifying_windows)} qualifying "
                f"weeks with positive distance found, {_TRAINING_LOAD_MIN_WEEKS} required",
            ),
        )
        return 1

    caveats: list[str] = []

    if partial_windows:
        caveats.append(
            "partial weeks detected: some weeks have long-run or pace data "
            "but missing distance metric"
        )

    pace_values = [v for w in qualifying_windows for v in w["pace"]]
    if not pace_values:
        caveats.append("missing average pace metric; using default 6:00/km estimate")
        avg_pace = _DEFAULT_PACE_SECONDS_PER_KM
    else:
        avg_pace = mean(pace_values)

    has_long_run = any(w["long_run"] for w in qualifying_windows)
    if not has_long_run:
        caveats.append("missing weekly long-run metric; projection based on pace and distance only")

    projected = avg_pace * _MARATHON_KM
    uncertainty = projected * _TRAINING_LOAD_UNCERTAINTY_FACTOR

    _upsert_projection_record(
        connection,
        record_id=record_id,
        method=TRAINING_LOAD_PROJECTION_METHOD,
        computed_at=now_utc,
        target_race_id=target_race_id,
        status=ProjectionStatus.ESTIMATED,
        projected_duration_seconds=projected,
        uncertainty_seconds=uncertainty,
        evidence_ids=tuple(all_evidence_ids),
        caveats=tuple(caveats),
    )
    return 1


def _upsert_projection_record(
    connection: sqlite3.Connection,
    *,
    record_id: str,
    method: MethodVersion,
    computed_at: datetime,
    target_race_id: str,
    status: ProjectionStatus,
    projected_duration_seconds: float | None,
    uncertainty_seconds: float | None,
    evidence_ids: tuple[str, ...],
    caveats: tuple[str, ...],
) -> None:
    ts = computed_at.isoformat()
    connection.execute(
        """
        INSERT INTO records (
            record_id, record_type, timezone, created_at, updated_at,
            provenance_kind, method_name, method_version
        ) VALUES (?, 'race_projection', 'UTC', ?, ?, 'computed', ?, ?)
        ON CONFLICT (record_id) DO UPDATE SET
            updated_at = excluded.updated_at,
            method_name = excluded.method_name,
            method_version = excluded.method_version
        """,
        (record_id, ts, ts, method.name, method.version),
    )
    connection.execute(
        "DELETE FROM provenance_evidence WHERE record_id = ?", (record_id,)
    )
    connection.executemany(
        """
        INSERT INTO provenance_evidence (record_id, evidence_record_id, position)
        VALUES (?, ?, ?)
        """,
        ((record_id, eid, pos) for pos, eid in enumerate(evidence_ids)),
    )
    connection.execute(
        "DELETE FROM provenance_caveats WHERE record_id = ?", (record_id,)
    )
    connection.executemany(
        """
        INSERT INTO provenance_caveats (record_id, position, caveat)
        VALUES (?, ?, ?)
        """,
        ((record_id, pos, c) for pos, c in enumerate(caveats)),
    )
    connection.execute(
        """
        INSERT INTO race_projections (
            record_id, target_race_id, status,
            projected_duration_seconds, uncertainty_seconds
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (record_id) DO UPDATE SET
            target_race_id = excluded.target_race_id,
            status = excluded.status,
            projected_duration_seconds = excluded.projected_duration_seconds,
            uncertainty_seconds = excluded.uncertainty_seconds
        """,
        (
            record_id,
            target_race_id,
            status.value,
            projected_duration_seconds,
            uncertainty_seconds,
        ),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
