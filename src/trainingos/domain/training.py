"""Race and training-block entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .common import (
    Measurement,
    RecordMetadata,
    Unit,
    as_utc,
    require_text,
    require_unit,
)


@dataclass(frozen=True, slots=True)
class Race:
    metadata: RecordMetadata
    name: str
    started_at: datetime
    distance: Measurement
    result_duration: Measurement | None = None
    target_duration: Measurement | None = None

    def __post_init__(self) -> None:
        require_text(self.name, "name")
        object.__setattr__(self, "started_at", as_utc(self.started_at, "started_at"))
        require_unit(self.distance, Unit.METRE, "distance")
        if self.distance.value <= 0:
            raise ValueError("race distance must be positive")
        if self.result_duration is not None:
            require_unit(self.result_duration, Unit.SECOND, "result_duration")
            if self.result_duration.value <= 0:
                raise ValueError("race result_duration must be positive")
        if self.target_duration is not None:
            require_unit(self.target_duration, Unit.SECOND, "target_duration")
            if self.target_duration.value <= 0:
                raise ValueError("race target_duration must be positive")


@dataclass(frozen=True, slots=True)
class TrainingBlock:
    metadata: RecordMetadata
    name: str
    start_date: date
    end_date: date
    target_race_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.name, "name")
        if self.end_date < self.start_date:
            raise ValueError("training block end_date must not precede start_date")
        if self.target_race_id is not None:
            require_text(self.target_race_id, "target_race_id")
