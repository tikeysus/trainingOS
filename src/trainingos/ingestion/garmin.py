"""Garmin source adapter seam and concrete garminconnect client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from trainingos.ingestion.sync import SyncError, SyncPage, SyncRecord

GARMIN_SOURCE = "garmin"
GARMIN_EMAIL_ENV = "TRAININGOS_GARMIN_EMAIL"
GARMIN_PASSWORD_ENV = "TRAININGOS_GARMIN_PASSWORD"

try:
    from garminconnect import Garmin
except ImportError:
    Garmin = None  # type: ignore[assignment,misc]


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


class GarminConnectClient:
    """Concrete GarminClient backed by the garminconnect library."""

    def __init__(self) -> None:
        email = os.environ.get(GARMIN_EMAIL_ENV, "")
        password = os.environ.get(GARMIN_PASSWORD_ENV, "")
        if not email:
            raise ValueError(f"{GARMIN_EMAIL_ENV} must be set and non-empty")
        if not password:
            raise ValueError(f"{GARMIN_PASSWORD_ENV} must be set and non-empty")
        try:
            self._garmin = Garmin(email, password)
            self._garmin.login()
        except Exception as exc:
            raise SyncError(
                "auth_failed",
                "Garmin authentication failed",
                retryable=True,
            ) from exc

    def fetch_activity_summaries(
        self,
        cursor: str | None,
        limit: int,
    ) -> GarminActivityPage:
        raw: list[dict] = self._garmin.get_activities(0, limit)
        activities = tuple(self._map(a) for a in raw)
        return GarminActivityPage(activities=activities, done=len(raw) < limit)

    def _map(self, raw: dict) -> GarminActivitySummary:
        if "activityId" not in raw:
            raise SyncError("missing_activity_id", "Garmin activity missing activityId")
        if "lastModified" not in raw:
            raise SyncError("missing_timestamp", "Garmin activity missing lastModified")
        external_id = str(raw["activityId"])
        updated_at = datetime.strptime(
            raw["lastModified"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC)
        return GarminActivitySummary(
            external_id=external_id,
            updated_at=updated_at,
            payload=raw,
        )


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
