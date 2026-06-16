"""Historical weather enrichment backed by local persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Protocol

from trainingos.domain import (
    Activity,
    Measurement,
    MethodVersion,
    MetricStatus,
    MetricValue,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    SourceReference,
    Unit,
    WeatherObservation,
)
from trainingos.ingestion.raw import RawArtifactStore
from trainingos.normalization import NormalizationStore


@dataclass(frozen=True, slots=True)
class WeatherRequest:
    activity_id: str
    started_at: datetime
    timezone: str


@dataclass(frozen=True, slots=True)
class WeatherFetchResult:
    external_id: str
    observed_at: datetime
    metrics: tuple[MetricValue, ...]
    condition: str | None = None
    raw_content: bytes | None = None
    content_type: str = "application/json"
    source_updated_at: datetime | None = None
    parser: MethodVersion | None = None

    @classmethod
    def unavailable(
        cls,
        *,
        external_id: str,
        observed_at: datetime,
        reason: str,
        raw_content: bytes | None = None,
    ) -> WeatherFetchResult:
        return cls(
            external_id=external_id,
            observed_at=observed_at,
            metrics=(
                MetricValue(
                    "weather",
                    None,
                    status=MetricStatus.UNAVAILABLE,
                    attributes=(("reason", reason),),
                ),
            ),
            raw_content=raw_content,
        )

    def __post_init__(self) -> None:
        if not self.external_id or not self.external_id.strip():
            raise ValueError("weather external_id must not be blank")
        if not self.content_type or not self.content_type.strip():
            raise ValueError("weather content_type must not be blank")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("weather observed_at must be timezone-aware")
        if not self.metrics:
            raise ValueError("weather fetch result requires at least one metric")


class HistoricalWeatherAdapter(Protocol):
    @property
    def source(self) -> str: ...

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult: ...


class WeatherEnricher:
    """Fetches workout-time weather and stores it for local-only reads."""

    def __init__(
        self,
        *,
        adapter: HistoricalWeatherAdapter,
        raw_store: RawArtifactStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._raw_store = raw_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def enrich_activity(
        self,
        connection: sqlite3.Connection,
        store: NormalizationStore,
        activity: Activity,
        *,
        sync_run_id: str,
    ) -> WeatherObservation:
        if not self._adapter.source or not self._adapter.source.strip():
            raise ValueError("weather adapter source must not be blank")
        now = _utc(self._clock(), "weather enrichment clock")
        request = WeatherRequest(
            activity_id=activity.metadata.record_id,
            started_at=activity.started_at,
            timezone=activity.metadata.timezone,
        )
        try:
            result = self._adapter.fetch(request)
        except Exception:
            result = WeatherFetchResult.unavailable(
                external_id=f"{activity.metadata.record_id}:weather",
                observed_at=activity.started_at,
                reason="adapter_error",
                raw_content=b'{"status":"unavailable","reason":"adapter_error"}',
            )
        raw_reference = self._retain_raw(connection, sync_run_id, result, now)
        updated_at = _utc(result.source_updated_at or now, "source_updated_at")
        observation = WeatherObservation(
            metadata=RecordMetadata(
                record_id=f"weather:{self._adapter.source}:{result.external_id}",
                timezone=activity.metadata.timezone,
                created_at=min(now, updated_at),
                updated_at=updated_at,
                source_references=(
                    SourceReference(
                        source=self._adapter.source,
                        external_id=result.external_id,
                        synced_at=now,
                        raw_reference=raw_reference,
                        parser=result.parser,
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            observed_at=result.observed_at,
            metrics=result.metrics,
            condition=result.condition,
        )
        store.upsert_weather_observation(
            observation,
            activity_id=activity.metadata.record_id,
        )
        return observation

    def _retain_raw(
        self,
        connection: sqlite3.Connection,
        sync_run_id: str,
        result: WeatherFetchResult,
        ingested_at: datetime,
    ) -> str:
        content = result.raw_content
        if content is None:
            content = _weather_raw_payload(result)
        artifact = self._raw_store.retain_bytes(
            connection,
            sync_run_id=sync_run_id,
            source=self._adapter.source,
            external_id=result.external_id,
            record_kind="weather_observation",
            content_type=result.content_type,
            content=content,
            source_updated_at=result.source_updated_at,
            ingested_at=ingested_at,
        )
        return artifact.raw_record_id


def _weather_raw_payload(result: WeatherFetchResult) -> bytes:
    status = "measured"
    if all(metric.status is not MetricStatus.MEASURED for metric in result.metrics):
        status = "unavailable"
    return json.dumps(
        {
            "external_id": result.external_id,
            "status": status,
            "observed_at": _utc(result.observed_at, "observed_at").isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8")


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def temperature_celsius(value: float, *, quality: float | None = None) -> MetricValue:
    return MetricValue(
        "temperature",
        Measurement(value, Unit.DEGREE_CELSIUS),
        quality=quality,
    )
