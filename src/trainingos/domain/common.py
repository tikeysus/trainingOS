"""Shared vendor-neutral domain primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Unit(StrEnum):
    METRE = "m"
    KILOMETRE = "km"
    SECOND = "s"
    METRE_PER_SECOND = "m/s"
    SECOND_PER_KILOMETRE = "s/km"
    BEAT_PER_MINUTE = "bpm"
    MILLISECOND = "ms"
    DEGREE_CELSIUS = "degC"
    PERCENT = "%"
    RATIO = "ratio"
    COUNT = "count"
    WATT = "W"
    MILLILITRE_PER_KILOGRAM_PER_MINUTE = "mL/kg/min"


class ProvenanceKind(StrEnum):
    OBSERVED = "observed"
    IMPORTED = "imported"
    USER_ENTERED = "user_entered"
    COMPUTED = "computed"
    MODEL_INTERPRETED = "model_interpreted"


class MetricStatus(StrEnum):
    MEASURED = "measured"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


def require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def validate_timezone(value: str) -> None:
    require_text(value, "timezone")
    try:
        zone = ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"timezone must be a valid IANA timezone: {value}") from error
    if zone.key != value:
        raise ValueError(f"timezone must use its canonical IANA key: {value}")


def require_unit(measurement: Measurement, expected: Unit, field_name: str) -> None:
    if measurement.unit is not expected:
        raise ValueError(f"{field_name} must use {expected.value}")


@dataclass(frozen=True, slots=True)
class Measurement:
    value: float
    unit: Unit

    def __post_init__(self) -> None:
        if not isfinite(self.value):
            raise ValueError("measurement value must be finite")


@dataclass(frozen=True, slots=True)
class SourceReference:
    source: str
    external_id: str
    synced_at: datetime
    raw_reference: str | None = None
    parser: MethodVersion | None = None

    def __post_init__(self) -> None:
        require_text(self.source, "source")
        require_text(self.external_id, "external_id")
        object.__setattr__(self, "synced_at", as_utc(self.synced_at, "synced_at"))
        if self.raw_reference is not None:
            require_text(self.raw_reference, "raw_reference")


@dataclass(frozen=True, slots=True)
class MethodVersion:
    name: str
    version: str

    def __post_init__(self) -> None:
        require_text(self.name, "method name")
        require_text(self.version, "method version")


@dataclass(frozen=True, slots=True)
class Provenance:
    kind: ProvenanceKind
    method: MethodVersion | None = None
    evidence_record_ids: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is ProvenanceKind.COMPUTED and self.method is None:
            raise ValueError("computed provenance requires a versioned method")
        if self.kind is not ProvenanceKind.COMPUTED and self.method is not None:
            raise ValueError("a versioned method is only valid for computed provenance")
        for record_id in self.evidence_record_ids:
            require_text(record_id, "evidence_record_id")
        for caveat in self.caveats:
            require_text(caveat, "caveat")


@dataclass(frozen=True, slots=True)
class RecordMetadata:
    record_id: str
    timezone: str
    created_at: datetime
    updated_at: datetime
    source_references: tuple[SourceReference, ...] = ()
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        validate_timezone(self.timezone)
        created_at = as_utc(self.created_at, "created_at")
        updated_at = as_utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class MetricValue:
    key: str
    measurement: Measurement | None
    quality: float | None = None
    attributes: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    status: MetricStatus = MetricStatus.MEASURED

    def __post_init__(self) -> None:
        require_text(self.key, "metric key")
        if self.status is MetricStatus.MEASURED and self.measurement is None:
            raise ValueError("measured metric requires a measurement")
        if self.status is not MetricStatus.MEASURED and self.measurement is not None:
            raise ValueError("unmeasured metric must not include a measurement")
        if self.status is not MetricStatus.MEASURED and self.quality is not None:
            raise ValueError("unmeasured metric must not include quality")
        if self.quality is not None and not 0.0 <= self.quality <= 1.0:
            raise ValueError("quality must be between 0 and 1")
        for key, value in self.attributes:
            require_text(key, "attribute key")
            require_text(value, "attribute value")
