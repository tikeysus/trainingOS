"""Daily health entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .common import MetricValue, RecordMetadata


@dataclass(frozen=True, slots=True)
class DailyHealth:
    metadata: RecordMetadata
    local_date: date
    metrics: tuple[MetricValue, ...]

    def __post_init__(self) -> None:
        if not self.metrics:
            raise ValueError("daily health requires at least one metric")
