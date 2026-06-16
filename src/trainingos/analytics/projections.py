"""Contracts for race projections derived from local evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trainingos.domain import MethodVersion
from trainingos.domain.common import require_text


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
