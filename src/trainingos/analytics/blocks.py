"""Deterministic, versioned block-level training metrics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from trainingos.domain import MethodVersion, Unit

BLOCK_TOTAL_MILEAGE_METHOD = MethodVersion("block_total_mileage", "1.0.0")
BLOCK_LONG_RUN_MAX_METHOD = MethodVersion("block_long_run_max", "1.0.0")
BLOCK_AVG_WEEKLY_DIST_METHOD = MethodVersion("block_average_weekly_distance", "1.0.0")
BLOCK_PEAK_WEEK_DIST_METHOD = MethodVersion("block_peak_week_distance", "1.0.0")
BLOCK_RECOVERY_COVERAGE_METHOD = MethodVersion("block_recovery_coverage", "1.0.0")
BLOCK_PHASE_MILEAGE_METHOD = MethodVersion("block_phase_mileage", "1.0.0")
BLOCK_MILEAGE_VS_PRIOR_METHOD = MethodVersion("block_total_mileage_vs_prior", "1.0.0")
BLOCK_LONG_RUN_VS_PRIOR_METHOD = MethodVersion("block_long_run_max_vs_prior", "1.0.0")


@dataclass(frozen=True, slots=True)
class BlockMetricsReport:
    block_count: int
    metric_count: int


@dataclass(frozen=True, slots=True)
class _ActivityInput:
    record_id: str
    started_at: datetime
    distance_metres: float | None


@dataclass(frozen=True, slots=True)
class _PhaseWindow:
    phase: str
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive


@dataclass(frozen=True, slots=True)
class _BlockData:
    record_id: str
    start_date: date
    end_date: date  # race_date if set, else ended_at.date() if set, else now.date()
    activities: tuple[_ActivityInput, ...]
    phase_windows: tuple[_PhaseWindow, ...]


def derive_block_metrics(
    connection: sqlite3.Connection,
    *,
    timezone: str,
    now: datetime,
) -> BlockMetricsReport:
    zone = ZoneInfo(timezone)
    computed_at = now.astimezone(UTC)
    blocks = _load_blocks(connection, zone, computed_at)
    if not blocks:
        return BlockMetricsReport(block_count=0, metric_count=0)

    metric_count = 0
    prior: _BlockData | None = None
    for block in sorted(blocks, key=lambda b: b.start_date):
        metric_count += _derive_for_block(connection, block, timezone, zone, computed_at)
        if prior is not None:
            metric_count += _derive_comparison(connection, block, prior, timezone, zone, computed_at)
        prior = block

    return BlockMetricsReport(block_count=len(blocks), metric_count=metric_count)


def _load_blocks(
    connection: sqlite3.Connection,
    zone: ZoneInfo,
    computed_at: datetime,
) -> tuple[_BlockData, ...]:
    rows = connection.execute(
        "SELECT record_id, start_date, race_date, ended_at FROM training_blocks"
    ).fetchall()
    result: list[_BlockData] = []
    for row in rows:
        start_date = date.fromisoformat(row["start_date"])
        if row["race_date"]:
            end_date = date.fromisoformat(row["race_date"])
        elif row["ended_at"]:
            ended_at = datetime.fromisoformat(row["ended_at"])
            end_date = ended_at.astimezone(zone).date()
        else:
            end_date = computed_at.astimezone(zone).date()

        activities = _load_activities(connection, start_date, end_date, zone)
        phase_windows = _load_phase_windows(connection, row["record_id"], start_date, end_date, zone)
        result.append(
            _BlockData(
                record_id=row["record_id"],
                start_date=start_date,
                end_date=end_date,
                activities=activities,
                phase_windows=phase_windows,
            )
        )
    return tuple(result)


def _load_activities(
    connection: sqlite3.Connection,
    start_date: date,
    end_date: date,
    zone: ZoneInfo,
) -> tuple[_ActivityInput, ...]:
    rows = connection.execute(
        """
        SELECT record_id, started_at, distance_metres
        FROM activities
        WHERE activity_type = 'run'
        ORDER BY started_at
        """
    ).fetchall()
    result: list[_ActivityInput] = []
    for row in rows:
        started_at = datetime.fromisoformat(row["started_at"])
        local_date = started_at.astimezone(zone).date()
        if start_date <= local_date <= end_date:
            result.append(
                _ActivityInput(
                    record_id=row["record_id"],
                    started_at=started_at.astimezone(UTC),
                    distance_metres=float(row["distance_metres"]) if row["distance_metres"] is not None else None,
                )
            )
    return tuple(result)


def _load_phase_windows(
    connection: sqlite3.Connection,
    block_id: str,
    start_date: date,
    end_date: date,
    zone: ZoneInfo,
) -> tuple[_PhaseWindow, ...]:
    rows = connection.execute(
        "SELECT phase, recorded_at FROM block_phase_transitions WHERE block_id = ? ORDER BY rowid",
        (block_id,),
    ).fetchall()
    if not rows:
        return ()

    block_end_utc = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    windows: list[_PhaseWindow] = []
    for i, row in enumerate(rows):
        phase_start = datetime.fromisoformat(row["recorded_at"]).astimezone(UTC)
        if i + 1 < len(rows):
            phase_end = datetime.fromisoformat(rows[i + 1]["recorded_at"]).astimezone(UTC)
        else:
            phase_end = block_end_utc
        windows.append(_PhaseWindow(phase=row["phase"], start=phase_start, end=phase_end))
    return tuple(windows)


def _block_window(block: _BlockData, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(block.start_date, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(block.end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def _derive_for_block(
    connection: sqlite3.Connection,
    block: _BlockData,
    timezone: str,
    zone: ZoneInfo,
    computed_at: datetime,
) -> int:
    activities = block.activities
    window_start, window_end = _block_window(block, timezone)
    metric_count = 0

    if not activities:
        # Recovery coverage still applies even with no runs
        metric_count += _persist_recovery_coverage(
            connection, block, timezone, zone, computed_at, window_start, window_end
        )
        return metric_count

    distances = [a.distance_metres for a in activities if a.distance_metres is not None]
    has_gaps = len(distances) != len(activities)
    evidence_ids = tuple(a.record_id for a in activities)
    caveats = ("one_or_more_runs_missing_distance",) if has_gaps else ()

    if distances:
        total = sum(distances)
        long_run = max(distances)

        metric_count += _persist_metric(
            connection,
            timezone=timezone,
            computed_at=computed_at,
            method=BLOCK_TOTAL_MILEAGE_METHOD,
            metric_key="block_total_mileage",
            value=total,
            unit=Unit.METRE,
            window_start=window_start,
            window_end=window_end,
            evidence_ids=evidence_ids,
            summary=f"Total block mileage: {total:.0f} m",
            record_id=f"block_metric:{BLOCK_TOTAL_MILEAGE_METHOD.name}:{BLOCK_TOTAL_MILEAGE_METHOD.version}:{block.record_id}",
            caveats=caveats,
        )
        metric_count += _persist_metric(
            connection,
            timezone=timezone,
            computed_at=computed_at,
            method=BLOCK_LONG_RUN_MAX_METHOD,
            metric_key="block_long_run_max",
            value=long_run,
            unit=Unit.METRE,
            window_start=window_start,
            window_end=window_end,
            evidence_ids=evidence_ids,
            summary=f"Longest run in block: {long_run:.0f} m",
            record_id=f"block_metric:{BLOCK_LONG_RUN_MAX_METHOD.name}:{BLOCK_LONG_RUN_MAX_METHOD.version}:{block.record_id}",
            caveats=caveats,
        )

        # Weekly aggregates
        weekly: dict[date, list[float]] = {}
        for activity in activities:
            if activity.distance_metres is None:
                continue
            local_date = activity.started_at.astimezone(zone).date()
            week_start = local_date - timedelta(days=local_date.weekday())
            weekly.setdefault(week_start, []).append(activity.distance_metres)

        if weekly:
            weekly_totals = [sum(dists) for dists in weekly.values()]
            avg_weekly = mean(weekly_totals)
            peak_weekly = max(weekly_totals)

            metric_count += _persist_metric(
                connection,
                timezone=timezone,
                computed_at=computed_at,
                method=BLOCK_AVG_WEEKLY_DIST_METHOD,
                metric_key="block_average_weekly_distance",
                value=avg_weekly,
                unit=Unit.METRE,
                window_start=window_start,
                window_end=window_end,
                evidence_ids=evidence_ids,
                summary=f"Average weekly distance: {avg_weekly:.0f} m",
                record_id=f"block_metric:{BLOCK_AVG_WEEKLY_DIST_METHOD.name}:{BLOCK_AVG_WEEKLY_DIST_METHOD.version}:{block.record_id}",
                caveats=caveats,
            )
            metric_count += _persist_metric(
                connection,
                timezone=timezone,
                computed_at=computed_at,
                method=BLOCK_PEAK_WEEK_DIST_METHOD,
                metric_key="block_peak_week_distance",
                value=peak_weekly,
                unit=Unit.METRE,
                window_start=window_start,
                window_end=window_end,
                evidence_ids=evidence_ids,
                summary=f"Peak week distance: {peak_weekly:.0f} m",
                record_id=f"block_metric:{BLOCK_PEAK_WEEK_DIST_METHOD.name}:{BLOCK_PEAK_WEEK_DIST_METHOD.version}:{block.record_id}",
                caveats=caveats,
            )

    # Per-phase mileage
    for pw in block.phase_windows:
        phase_activities = [
            a for a in activities
            if pw.start <= a.started_at < pw.end and a.distance_metres is not None
        ]
        if not phase_activities:
            continue
        phase_total = sum(a.distance_metres for a in phase_activities)  # type: ignore[arg-type]
        phase_evidence = tuple(a.record_id for a in phase_activities)
        metric_count += _persist_metric(
            connection,
            timezone=timezone,
            computed_at=computed_at,
            method=BLOCK_PHASE_MILEAGE_METHOD,
            metric_key=f"block_{pw.phase}_mileage",
            value=phase_total,
            unit=Unit.METRE,
            window_start=pw.start,
            window_end=pw.end,
            evidence_ids=phase_evidence,
            summary=f"{pw.phase.capitalize()} phase mileage: {phase_total:.0f} m",
            record_id=f"block_metric:{BLOCK_PHASE_MILEAGE_METHOD.name}:{BLOCK_PHASE_MILEAGE_METHOD.version}:{block.record_id}:{pw.phase}",
        )

    # Recovery coverage
    metric_count += _persist_recovery_coverage(
        connection, block, timezone, zone, computed_at, window_start, window_end
    )

    return metric_count


def _persist_recovery_coverage(
    connection: sqlite3.Connection,
    block: _BlockData,
    timezone: str,
    zone: ZoneInfo,
    computed_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    total_days = (block.end_date - block.start_date).days + 1
    local_dates = {
        (block.start_date + timedelta(days=i)).isoformat() for i in range(total_days)
    }
    placeholders = ",".join("?" for _ in local_dates)
    rows = connection.execute(
        f"""
        SELECT daily.record_id
        FROM daily_health AS daily
        JOIN metric_values AS metric
          ON metric.owner_record_id = daily.record_id
        WHERE daily.local_date IN ({placeholders})
          AND metric.metric_key = 'sleep_duration'
          AND metric.metric_status = 'measured'
        GROUP BY daily.record_id
        """,
        tuple(sorted(local_dates)),
    ).fetchall()
    covered_days = len(rows)
    coverage = covered_days / total_days
    evidence_ids = tuple(row["record_id"] for row in rows)
    all_activity_ids = tuple(a.record_id for a in block.activities)
    fallback_evidence = evidence_ids if evidence_ids else all_activity_ids

    return _persist_metric(
        connection,
        timezone=timezone,
        computed_at=computed_at,
        method=BLOCK_RECOVERY_COVERAGE_METHOD,
        metric_key="block_recovery_coverage",
        value=coverage,
        unit=Unit.RATIO,
        window_start=window_start,
        window_end=window_end,
        evidence_ids=fallback_evidence,
        summary=f"{covered_days} of {total_days} block days have recovery evidence",
        record_id=f"block_metric:{BLOCK_RECOVERY_COVERAGE_METHOD.name}:{BLOCK_RECOVERY_COVERAGE_METHOD.version}:{block.record_id}",
        quality=coverage,
        caveats=() if coverage == 1.0 else ("recovery coverage is incomplete",),
    )


def _derive_comparison(
    connection: sqlite3.Connection,
    block: _BlockData,
    prior: _BlockData,
    timezone: str,
    zone: ZoneInfo,
    computed_at: datetime,
) -> int:
    window_start, window_end = _block_window(block, timezone)
    metric_count = 0

    # Total mileage comparison
    current_dists = [a.distance_metres for a in block.activities if a.distance_metres is not None]
    prior_dists = [a.distance_metres for a in prior.activities if a.distance_metres is not None]
    if current_dists and prior_dists:
        current_total = sum(current_dists)
        prior_total = sum(prior_dists)
        if prior_total > 0:
            ratio = current_total / prior_total
            evidence = tuple(a.record_id for a in block.activities) + tuple(a.record_id for a in prior.activities)
            metric_count += _persist_metric(
                connection,
                timezone=timezone,
                computed_at=computed_at,
                method=BLOCK_MILEAGE_VS_PRIOR_METHOD,
                metric_key="block_total_mileage_vs_prior",
                value=ratio,
                unit=Unit.RATIO,
                window_start=window_start,
                window_end=window_end,
                evidence_ids=evidence,
                summary=f"Current block total mileage vs prior: {ratio:.2f}x",
                record_id=f"block_metric:{BLOCK_MILEAGE_VS_PRIOR_METHOD.name}:{BLOCK_MILEAGE_VS_PRIOR_METHOD.version}:{block.record_id}",
            )

    # Long run max comparison
    if current_dists and prior_dists:
        current_long = max(current_dists)
        prior_long = max(prior_dists)
        if prior_long > 0:
            ratio = current_long / prior_long
            evidence = tuple(a.record_id for a in block.activities) + tuple(a.record_id for a in prior.activities)
            metric_count += _persist_metric(
                connection,
                timezone=timezone,
                computed_at=computed_at,
                method=BLOCK_LONG_RUN_VS_PRIOR_METHOD,
                metric_key="block_long_run_max_vs_prior",
                value=ratio,
                unit=Unit.RATIO,
                window_start=window_start,
                window_end=window_end,
                evidence_ids=evidence,
                summary=f"Current block long run vs prior: {ratio:.2f}x",
                record_id=f"block_metric:{BLOCK_LONG_RUN_VS_PRIOR_METHOD.name}:{BLOCK_LONG_RUN_VS_PRIOR_METHOD.version}:{block.record_id}",
            )

    return metric_count


# ---------------------------------------------------------------------------
# Helpers (adapted from analytics/metrics.py)
# ---------------------------------------------------------------------------

def _persist_metric(
    connection: sqlite3.Connection,
    *,
    timezone: str,
    computed_at: datetime,
    method: MethodVersion,
    metric_key: str,
    value: float,
    unit: Unit,
    window_start: datetime,
    window_end: datetime,
    evidence_ids: tuple[str, ...],
    summary: str,
    record_id: str,
    quality: float | None = None,
    caveats: tuple[str, ...] = (),
) -> int:
    _upsert_computed_record(
        connection,
        record_id=record_id,
        record_type="metric_evidence",
        timezone=timezone,
        computed_at=computed_at,
        method=method,
        evidence_ids=evidence_ids,
        caveats=caveats,
    )
    connection.execute(
        """
        INSERT INTO metric_evidence (
            record_id, metric_key, metric_value, unit, quality,
            window_start, window_end, summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (record_id) DO UPDATE SET
            metric_key = excluded.metric_key,
            metric_value = excluded.metric_value,
            unit = excluded.unit,
            quality = excluded.quality,
            window_start = excluded.window_start,
            window_end = excluded.window_end,
            summary = excluded.summary
        """,
        (
            record_id,
            metric_key,
            value,
            unit.value,
            quality,
            window_start.isoformat(),
            window_end.isoformat(),
            summary,
        ),
    )
    return 1


def _upsert_computed_record(
    connection: sqlite3.Connection,
    *,
    record_id: str,
    record_type: str,
    timezone: str,
    computed_at: datetime,
    method: MethodVersion,
    evidence_ids: tuple[str, ...],
    caveats: tuple[str, ...],
) -> None:
    connection.execute(
        """
        INSERT INTO records (
            record_id, record_type, timezone, created_at, updated_at,
            provenance_kind, method_name, method_version
        ) VALUES (?, ?, ?, ?, ?, 'computed', ?, ?)
        ON CONFLICT (record_id) DO UPDATE SET
            record_type = excluded.record_type,
            timezone = excluded.timezone,
            updated_at = excluded.updated_at,
            provenance_kind = excluded.provenance_kind,
            method_name = excluded.method_name,
            method_version = excluded.method_version
        """,
        (
            record_id,
            record_type,
            timezone,
            computed_at.isoformat(),
            computed_at.isoformat(),
            method.name,
            method.version,
        ),
    )
    connection.execute(
        "DELETE FROM provenance_evidence WHERE record_id = ?",
        (record_id,),
    )
    connection.executemany(
        """
        INSERT INTO provenance_evidence (
            record_id, evidence_record_id, position
        ) VALUES (?, ?, ?)
        """,
        (
            (record_id, eid, pos)
            for pos, eid in enumerate(evidence_ids)
        ),
    )
    connection.execute(
        "DELETE FROM provenance_caveats WHERE record_id = ?",
        (record_id,),
    )
    connection.executemany(
        """
        INSERT INTO provenance_caveats (record_id, position, caveat)
        VALUES (?, ?, ?)
        """,
        (
            (record_id, pos, caveat)
            for pos, caveat in enumerate(caveats)
        ),
    )
