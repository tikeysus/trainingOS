"""Deterministic, versioned training metrics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from trainingos.domain import MethodVersion, Unit

WEEKLY_SUMMARY_METHOD = MethodVersion("weekly_summary", "1.0.0")
WEEKLY_DISTANCE_METHOD = MethodVersion("weekly_distance", "1.0.0")
WEEKLY_DURATION_METHOD = MethodVersion("weekly_duration", "1.0.0")
WEEKLY_RUN_COUNT_METHOD = MethodVersion("weekly_run_count", "1.0.0")
WEEKLY_LONG_RUN_METHOD = MethodVersion("weekly_long_run", "1.0.0")
WEEKLY_AVERAGE_PACE_METHOD = MethodVersion("weekly_average_pace", "1.0.0")
AEROBIC_EFFICIENCY_METHOD = MethodVersion("aerobic_efficiency", "1.0.0")
HEART_RATE_DRIFT_METHOD = MethodVersion("heart_rate_drift", "1.0.0")
RECOVERY_COVERAGE_METHOD = MethodVersion("recovery_coverage", "1.0.0")
LOAD_TREND_METHOD = MethodVersion("training_load_trend", "1.0.0")
MARATHON_PACE_VOLUME_METHOD = MethodVersion("marathon_pace_volume", "1.0.0")

MARATHON_DISTANCE_METRES = 42195.0
TARGET_MARATHON_DURATION_SECONDS = 3 * 3600 + 10 * 60
TARGET_MARATHON_PACE_SECONDS_PER_KILOMETRE = (
    TARGET_MARATHON_DURATION_SECONDS / (MARATHON_DISTANCE_METRES / 1000.0)
)
MARATHON_PACE_TOLERANCE = 0.05


@dataclass(frozen=True, slots=True)
class DerivationReport:
    timezone: str
    week_count: int
    metric_count: int


@dataclass(frozen=True, slots=True)
class ActivityInput:
    record_id: str
    started_at: datetime
    duration_seconds: float
    distance_metres: float | None


@dataclass(frozen=True, slots=True)
class WeekInput:
    week_start: date
    week_end: date
    activities: tuple[ActivityInput, ...]


def derive_training_metrics(
    connection: sqlite3.Connection,
    *,
    timezone: str = "America/Toronto",
    now: datetime | None = None,
) -> DerivationReport:
    """Compute request-path-independent training metrics from local SQLite data."""
    zone = ZoneInfo(timezone)
    computed_at = _utc(now or datetime.now(UTC), "now")
    weeks = _load_weeks(connection, zone)
    metric_count = 0
    for week in weeks:
        metric_count += _persist_week(connection, week, timezone, computed_at)
    metric_count += _persist_load_trends(connection, weeks, timezone, computed_at)
    return DerivationReport(
        timezone=timezone,
        week_count=len(weeks),
        metric_count=metric_count,
    )


def _load_weeks(connection: sqlite3.Connection, zone: ZoneInfo) -> tuple[WeekInput, ...]:
    rows = connection.execute(
        """
        SELECT record_id, started_at, duration_seconds, distance_metres
        FROM activities
        WHERE activity_type = 'run'
        ORDER BY started_at, record_id
        """
    ).fetchall()
    by_week: dict[date, list[ActivityInput]] = {}
    for row in rows:
        started_at = datetime.fromisoformat(row["started_at"])
        local_date = started_at.astimezone(zone).date()
        week_start = local_date - timedelta(days=local_date.weekday())
        by_week.setdefault(week_start, []).append(
            ActivityInput(
                record_id=row["record_id"],
                started_at=started_at.astimezone(UTC),
                duration_seconds=float(row["duration_seconds"]),
                distance_metres=None
                if row["distance_metres"] is None
                else float(row["distance_metres"]),
            )
        )
    return tuple(
        WeekInput(
            week_start=week_start,
            week_end=week_start + timedelta(days=6),
            activities=tuple(activities),
        )
        for week_start, activities in sorted(by_week.items())
    )


def _persist_week(
    connection: sqlite3.Connection,
    week: WeekInput,
    timezone: str,
    computed_at: datetime,
) -> int:
    evidence_ids = tuple(activity.record_id for activity in week.activities)
    distances = tuple(
        activity.distance_metres
        for activity in week.activities
        if activity.distance_metres is not None
    )
    data_gaps: list[str] = []
    if len(distances) != len(week.activities):
        data_gaps.append("one_or_more_runs_missing_distance")
    run_count = len(week.activities)
    duration_seconds = sum(activity.duration_seconds for activity in week.activities)
    distance_metres = sum(distances)
    long_run_metres = max(distances) if distances else None
    average_pace = (
        duration_seconds / (distance_metres / 1000.0)
        if distance_metres > 0 and duration_seconds > 0
        else None
    )
    week_record_id = f"weekly_summary:{timezone}:{week.week_start.isoformat()}"
    _upsert_computed_record(
        connection,
        record_id=week_record_id,
        record_type="weekly_summary",
        timezone=timezone,
        computed_at=computed_at,
        method=WEEKLY_SUMMARY_METHOD,
        evidence_ids=evidence_ids,
        caveats=tuple(data_gaps),
    )
    connection.execute(
        """
        INSERT INTO weekly_summaries (
            record_id, week_start_date, week_end_date, timezone, run_count,
            distance_metres, duration_seconds, long_run_metres,
            average_pace_seconds_per_kilometre, data_gaps_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (record_id) DO UPDATE SET
            week_start_date = excluded.week_start_date,
            week_end_date = excluded.week_end_date,
            timezone = excluded.timezone,
            run_count = excluded.run_count,
            distance_metres = excluded.distance_metres,
            duration_seconds = excluded.duration_seconds,
            long_run_metres = excluded.long_run_metres,
            average_pace_seconds_per_kilometre =
                excluded.average_pace_seconds_per_kilometre,
            data_gaps_json = excluded.data_gaps_json
        """,
        (
            week_record_id,
            week.week_start.isoformat(),
            week.week_end.isoformat(),
            timezone,
            run_count,
            distance_metres,
            duration_seconds,
            long_run_metres,
            average_pace,
            json.dumps(data_gaps, sort_keys=True),
        ),
    )
    metric_count = 0
    window_start, window_end = _week_window(week, timezone)
    metric_count += _persist_metric(
        connection,
        timezone=timezone,
        computed_at=computed_at,
        method=WEEKLY_RUN_COUNT_METHOD,
        metric_key="weekly_run_count",
        value=run_count,
        unit=Unit.COUNT,
        window_start=window_start,
        window_end=window_end,
        evidence_ids=evidence_ids,
        summary=f"{run_count} runs in week starting {week.week_start.isoformat()}",
    )
    metric_count += _persist_metric(
        connection,
        timezone=timezone,
        computed_at=computed_at,
        method=WEEKLY_DURATION_METHOD,
        metric_key="weekly_duration",
        value=duration_seconds,
        unit=Unit.SECOND,
        window_start=window_start,
        window_end=window_end,
        evidence_ids=evidence_ids,
        summary=f"{duration_seconds:.0f} seconds of running",
    )
    if len(distances) == len(week.activities):
        metric_count += _persist_metric(
            connection,
            timezone=timezone,
            computed_at=computed_at,
            method=WEEKLY_DISTANCE_METHOD,
            metric_key="weekly_distance",
            value=distance_metres,
            unit=Unit.METRE,
            window_start=window_start,
            window_end=window_end,
            evidence_ids=evidence_ids,
            summary=f"{distance_metres:.0f} metres of running",
        )
        if long_run_metres is not None:
            metric_count += _persist_metric(
                connection,
                timezone=timezone,
                computed_at=computed_at,
                method=WEEKLY_LONG_RUN_METHOD,
                metric_key="weekly_long_run",
                value=long_run_metres,
                unit=Unit.METRE,
                window_start=window_start,
                window_end=window_end,
                evidence_ids=evidence_ids,
                summary=f"{long_run_metres:.0f} metre long run",
            )
        if average_pace is not None:
            metric_count += _persist_metric(
                connection,
                timezone=timezone,
                computed_at=computed_at,
                method=WEEKLY_AVERAGE_PACE_METHOD,
                metric_key="weekly_average_pace",
                value=average_pace,
                unit=Unit.SECOND_PER_KILOMETRE,
                window_start=window_start,
                window_end=window_end,
                evidence_ids=evidence_ids,
                summary=f"{average_pace:.1f} seconds per kilometre average pace",
            )
    metric_count += _persist_optional_aerobic_efficiency(
        connection, week, timezone, computed_at, window_start, window_end
    )
    metric_count += _persist_optional_heart_rate_drift(
        connection, week, timezone, computed_at
    )
    metric_count += _persist_recovery_coverage(
        connection, week, timezone, computed_at, window_start, window_end
    )
    metric_count += _persist_marathon_pace_volume(
        connection, week, timezone, computed_at, window_start, window_end
    )
    return metric_count


def _persist_load_trends(
    connection: sqlite3.Connection,
    weeks: tuple[WeekInput, ...],
    timezone: str,
    computed_at: datetime,
) -> int:
    count = 0
    complete_week_distances: dict[date, float] = {}
    for week in weeks:
        distances = tuple(
            activity.distance_metres
            for activity in week.activities
            if activity.distance_metres is not None
        )
        if len(distances) == len(week.activities):
            complete_week_distances[week.week_start] = sum(distances)
    for index, week in enumerate(weeks):
        prior_weeks = weeks[max(0, index - 4) : index]
        prior_distances = [
            complete_week_distances[prior.week_start]
            for prior in prior_weeks
            if prior.week_start in complete_week_distances
        ]
        current_distance = complete_week_distances.get(week.week_start)
        if current_distance is None or not prior_distances:
            continue
        baseline = mean(prior_distances)
        if baseline <= 0:
            continue
        window_start, window_end = _week_window(week, timezone)
        expected_prior_count = min(4, index)
        count += _persist_metric(
            connection,
            timezone=timezone,
            computed_at=computed_at,
            method=LOAD_TREND_METHOD,
            metric_key="training_load_trend",
            value=current_distance / baseline,
            unit=Unit.RATIO,
            window_start=window_start,
            window_end=window_end,
            evidence_ids=tuple(
                activity.record_id
                for trend_week in (*prior_weeks, week)
                for activity in trend_week.activities
            ),
            summary="Current weekly distance divided by recent prior-week average",
            quality=len(prior_distances) / expected_prior_count,
            caveats=("less than four prior complete weeks available",)
            if expected_prior_count < 4
            else (),
        )
    return count


def _persist_optional_aerobic_efficiency(
    connection: sqlite3.Connection,
    week: WeekInput,
    timezone: str,
    computed_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    values: list[float] = []
    evidence_ids: list[str] = []
    for activity in week.activities:
        if activity.distance_metres is None or activity.duration_seconds <= 0:
            continue
        heart_rates = _activity_metric_values(connection, activity.record_id, "heart_rate")
        if not heart_rates:
            continue
        average_hr = mean(heart_rates)
        if average_hr <= 0:
            continue
        speed = activity.distance_metres / activity.duration_seconds
        values.append(speed / average_hr)
        evidence_ids.append(activity.record_id)
    if not values:
        return 0
    return _persist_metric(
        connection,
        timezone=timezone,
        computed_at=computed_at,
        method=AEROBIC_EFFICIENCY_METHOD,
        metric_key="aerobic_efficiency",
        value=mean(values),
        unit=Unit.RATIO,
        window_start=window_start,
        window_end=window_end,
        evidence_ids=tuple(evidence_ids),
        summary="Average running speed divided by average heart rate",
        quality=len(values) / len(week.activities),
        caveats=("lower quality because some runs lack heart-rate samples",)
        if len(values) != len(week.activities)
        else (),
    )


def _persist_optional_heart_rate_drift(
    connection: sqlite3.Connection,
    week: WeekInput,
    timezone: str,
    computed_at: datetime,
) -> int:
    count = 0
    for activity in week.activities:
        rows = connection.execute(
            """
            SELECT sample.recorded_at, metric.metric_value
            FROM activity_samples AS sample
            JOIN metric_values AS metric
              ON metric.owner_record_id = sample.record_id
            WHERE sample.activity_id = ?
              AND metric.metric_key = 'heart_rate'
              AND metric.metric_status = 'measured'
            ORDER BY sample.recorded_at
            """,
            (activity.record_id,),
        ).fetchall()
        if len(rows) < 4:
            continue
        midpoint = len(rows) // 2
        first_half = [float(row["metric_value"]) for row in rows[:midpoint]]
        second_half = [float(row["metric_value"]) for row in rows[midpoint:]]
        first_average = mean(first_half)
        if first_average <= 0:
            continue
        drift = ((mean(second_half) - first_average) / first_average) * 100.0
        start = datetime.fromisoformat(rows[0]["recorded_at"]).astimezone(UTC)
        end = datetime.fromisoformat(rows[-1]["recorded_at"]).astimezone(UTC)
        count += _persist_metric(
            connection,
            timezone=timezone,
            computed_at=computed_at,
            method=HEART_RATE_DRIFT_METHOD,
            metric_key="heart_rate_drift",
            value=drift,
            unit=Unit.PERCENT,
            window_start=start,
            window_end=end,
            evidence_ids=(activity.record_id,),
            summary="Percent change from first-half to second-half average heart rate",
        )
    return count


def _persist_recovery_coverage(
    connection: sqlite3.Connection,
    week: WeekInput,
    timezone: str,
    computed_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    local_dates = {
        (week.week_start + timedelta(days=offset)).isoformat() for offset in range(7)
    }
    placeholders = ",".join("?" for _ in local_dates)
    rows = connection.execute(
        f"""
        SELECT daily.record_id
        FROM daily_health AS daily
        JOIN metric_values AS metric
          ON metric.owner_record_id = daily.record_id
        WHERE daily.local_date IN ({placeholders})
          AND metric.metric_key IN ('sleep_duration', 'hrv', 'resting_heart_rate')
          AND metric.metric_status = 'measured'
        GROUP BY daily.record_id
        """,
        tuple(sorted(local_dates)),
    ).fetchall()
    evidence_ids = tuple(row["record_id"] for row in rows)
    coverage = len(evidence_ids) / 7.0
    caveats = () if coverage == 1.0 else ("recovery coverage is incomplete",)
    return _persist_metric(
        connection,
        timezone=timezone,
        computed_at=computed_at,
        method=RECOVERY_COVERAGE_METHOD,
        metric_key="recovery_coverage",
        value=coverage,
        unit=Unit.RATIO,
        window_start=window_start,
        window_end=window_end,
        evidence_ids=evidence_ids or tuple(activity.record_id for activity in week.activities),
        summary=f"{len(evidence_ids)} of 7 days have recovery evidence",
        quality=coverage,
        caveats=caveats,
    )


def _persist_marathon_pace_volume(
    connection: sqlite3.Connection,
    week: WeekInput,
    timezone: str,
    computed_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    activity_ids = tuple(activity.record_id for activity in week.activities)
    if not activity_ids:
        return 0
    placeholders = ",".join("?" for _ in activity_ids)
    rows = connection.execute(
        f"""
        SELECT record_id, duration_seconds, distance_metres
        FROM laps
        WHERE activity_id IN ({placeholders})
          AND distance_metres IS NOT NULL
          AND duration_seconds > 0
          AND distance_metres > 0
        """,
        activity_ids,
    ).fetchall()
    matching_lap_ids: list[str] = []
    total_distance = 0.0
    lower = TARGET_MARATHON_PACE_SECONDS_PER_KILOMETRE * (
        1.0 - MARATHON_PACE_TOLERANCE
    )
    upper = TARGET_MARATHON_PACE_SECONDS_PER_KILOMETRE * (
        1.0 + MARATHON_PACE_TOLERANCE
    )
    for row in rows:
        distance = float(row["distance_metres"])
        pace = float(row["duration_seconds"]) / (distance / 1000.0)
        if lower <= pace <= upper:
            matching_lap_ids.append(row["record_id"])
            total_distance += distance
    if not matching_lap_ids:
        return 0
    return _persist_metric(
        connection,
        timezone=timezone,
        computed_at=computed_at,
        method=MARATHON_PACE_VOLUME_METHOD,
        metric_key="marathon_pace_volume",
        value=total_distance,
        unit=Unit.METRE,
        window_start=window_start,
        window_end=window_end,
        evidence_ids=tuple(matching_lap_ids),
        summary="Lap distance within +/-5% of 3:10 marathon pace",
        caveats=("uses configured v1 3:10 marathon-pace target",),
    )


def _activity_metric_values(
    connection: sqlite3.Connection,
    activity_id: str,
    metric_key: str,
) -> tuple[float, ...]:
    rows = connection.execute(
        """
        SELECT metric.metric_value
        FROM activity_samples AS sample
        JOIN metric_values AS metric
          ON metric.owner_record_id = sample.record_id
        WHERE sample.activity_id = ?
          AND metric.metric_key = ?
          AND metric.metric_status = 'measured'
        ORDER BY sample.recorded_at
        """,
        (activity_id, metric_key),
    ).fetchall()
    return tuple(float(row["metric_value"]) for row in rows)


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
    quality: float | None = None,
    caveats: tuple[str, ...] = (),
) -> int:
    record_id = (
        f"metric:{method.name}:{method.version}:"
        f"{window_start.isoformat()}:{window_end.isoformat()}"
    )
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
            (record_id, evidence_id, position)
            for position, evidence_id in enumerate(evidence_ids)
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
            (record_id, position, caveat)
            for position, caveat in enumerate(caveats)
        ),
    )


def _week_window(week: WeekInput, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(week.week_start, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(
        week.week_end + timedelta(days=1),
        time.min,
        tzinfo=zone,
    ).astimezone(UTC)
    return start, end


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
