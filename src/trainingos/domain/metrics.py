"""Versioned derived metric evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .common import (
    MetricStatus,
    MetricValue,
    ProvenanceKind,
    RecordMetadata,
    as_utc,
    require_text,
)


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    metadata: RecordMetadata
    metric: MetricValue
    window_start: datetime
    window_end: datetime
    summary: str | None = None

    def __post_init__(self) -> None:
        window_start = as_utc(self.window_start, "window_start")
        window_end = as_utc(self.window_end, "window_end")
        if window_end < window_start:
            raise ValueError("window_end must not precede window_start")
        if self.metadata.provenance is None:
            raise ValueError("metric evidence requires provenance")
        if self.metadata.provenance.kind is not ProvenanceKind.COMPUTED:
            raise ValueError("metric evidence must use computed provenance")
        if self.metric.status is not MetricStatus.MEASURED:
            raise ValueError("metric evidence requires a measured value")
        if self.summary is not None:
            require_text(self.summary, "summary")
        object.__setattr__(self, "window_start", window_start)
        object.__setattr__(self, "window_end", window_end)
