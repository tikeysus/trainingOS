"""Activity, lap, and high-resolution sample entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .common import (
    Measurement,
    MetricValue,
    RecordMetadata,
    Unit,
    as_utc,
    require_text,
    require_unit,
)


class ActivityType(StrEnum):
    RUN = "run"
    WALK = "walk"
    RIDE = "ride"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Activity:
    metadata: RecordMetadata
    activity_type: ActivityType
    started_at: datetime
    duration: Measurement
    distance: Measurement | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", as_utc(self.started_at, "started_at"))
        require_unit(self.duration, Unit.SECOND, "duration")
        if self.duration.value < 0:
            raise ValueError("duration must not be negative")
        if self.distance is not None:
            require_unit(self.distance, Unit.METRE, "distance")
            if self.distance.value < 0:
                raise ValueError("distance must not be negative")
        if self.title is not None:
            require_text(self.title, "title")


@dataclass(frozen=True, slots=True)
class Lap:
    metadata: RecordMetadata
    activity_id: str
    index: int
    started_at: datetime
    duration: Measurement
    distance: Measurement | None = None

    def __post_init__(self) -> None:
        require_text(self.activity_id, "activity_id")
        if self.index < 0:
            raise ValueError("lap index must not be negative")
        object.__setattr__(self, "started_at", as_utc(self.started_at, "started_at"))
        require_unit(self.duration, Unit.SECOND, "duration")
        if self.duration.value < 0:
            raise ValueError("duration must not be negative")
        if self.distance is not None:
            require_unit(self.distance, Unit.METRE, "distance")
            if self.distance.value < 0:
                raise ValueError("distance must not be negative")


@dataclass(frozen=True, slots=True)
class ActivitySample:
    metadata: RecordMetadata
    activity_id: str
    recorded_at: datetime
    metrics: tuple[MetricValue, ...]

    def __post_init__(self) -> None:
        require_text(self.activity_id, "activity_id")
        object.__setattr__(self, "recorded_at", as_utc(self.recorded_at, "recorded_at"))
        if not self.metrics:
            raise ValueError("activity sample requires at least one metric")
