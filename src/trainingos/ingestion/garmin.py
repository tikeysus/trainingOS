"""Garmin source adapter seam without a live client dependency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from trainingos.ingestion.sync import SyncPage, SyncRecord

GARMIN_SOURCE = "garmin"


@dataclass(frozen=True, slots=True)
class GarminActivitySummary:
    external_id: str
    updated_at: datetime
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if not self.external_id or not self.external_id.strip():
            raise ValueError("external_id must not be blank")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class GarminActivityPage:
    activities: tuple[GarminActivitySummary, ...]
    done: bool


class GarminClient(Protocol):
    def fetch_activity_summaries(
        self,
        cursor: str | None,
        limit: int,
    ) -> GarminActivityPage: ...


class GarminActivityAdapter:
    source = GARMIN_SOURCE

    def __init__(self, client: GarminClient) -> None:
        self._client = client

    def fetch(
        self,
        cursor: str | None,
        limit: int,
    ) -> SyncPage[GarminActivitySummary]:
        page = self._client.fetch_activity_summaries(cursor, limit)
        records = tuple(
            SyncRecord(
                external_id=activity.external_id,
                cursor_after=_cursor(activity),
                payload=activity,
            )
            for activity in page.activities
        )
        return SyncPage(records=records, done=page.done)

    def cursor_is_at_or_after(self, previous: str, candidate: str) -> bool:
        previous_time, previous_id = _split_cursor(previous)
        candidate_time, candidate_id = _split_cursor(candidate)
        return (candidate_time, candidate_id) >= (previous_time, previous_id)


def _cursor(activity: GarminActivitySummary) -> str:
    updated_at = activity.updated_at.astimezone(UTC).isoformat()
    return f"{updated_at}|{activity.external_id}"


def _split_cursor(value: str) -> tuple[datetime, str]:
    timestamp, separator, external_id = value.partition("|")
    if not separator or not external_id:
        raise ValueError("invalid Garmin activity cursor")
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid Garmin activity cursor timestamp")
    return parsed.astimezone(UTC), external_id
