"""User context and workout-time weather entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .common import MetricValue, RecordMetadata, as_utc, require_text


class NoteKind(StrEnum):
    WORKOUT = "workout"
    ILLNESS = "illness"
    INJURY = "injury"
    TRAVEL = "travel"
    STRESS = "stress"
    NOTE = "note"
    RACE = "race"
    TRAINING_BLOCK = "training_block"
    GENERAL = "general"


@dataclass(frozen=True, slots=True)
class ContextNote:
    metadata: RecordMetadata
    occurred_at: datetime
    kind: NoteKind
    text: str
    linked_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", as_utc(self.occurred_at, "occurred_at"))
        require_text(self.text, "text")
        for record_id in self.linked_record_ids:
            require_text(record_id, "linked_record_id")


@dataclass(frozen=True, slots=True)
class WeatherObservation:
    metadata: RecordMetadata
    observed_at: datetime
    metrics: tuple[MetricValue, ...]
    condition: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", as_utc(self.observed_at, "observed_at"))
        if not self.metrics:
            raise ValueError("weather observation requires at least one metric")
        if self.condition is not None:
            require_text(self.condition, "condition")
