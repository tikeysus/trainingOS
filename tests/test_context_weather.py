from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from trainingos.domain import (
    Activity,
    ActivityType,
    ContextNote,
    Measurement,
    MetricStatus,
    MetricValue,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    SourceReference,
    Unit,
    WeatherObservation,
)
from trainingos.ingestion import (
    RawArtifactStore,
    WeatherEnricher,
    WeatherFetchResult,
    WeatherRequest,
    temperature_celsius,
)
from trainingos.normalization import NormalizationStore
from trainingos.storage import apply_migrations, connect_database


class ContextWeatherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-weather-1', 'fixture_weather', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-activity-1', 'fixture', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, payload, ingested_at
            ) VALUES (
                'raw-activity-1', 'run-activity-1', 'fixture', 'activity-1',
                'activity_file', 'application/octet-stream', 'sha256:activity',
                X'00', '2026-11-01T07:00:00+00:00'
            )
            """
        )
        self.connection.commit()
        self.store = NormalizationStore(self.connection)
        self.raw_store = RawArtifactStore(root / "raw")
        self.activity = self._activity()
        self.store.upsert_activity(self.activity)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_user_entered_context_note_is_editable_and_linked(self) -> None:
        note = self._note(
            text="Left calf tight after strides.",
            linked_record_ids=("activity-1",),
            updated_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        )
        self.store.upsert_context_note(note)
        self.store.upsert_context_note(note)
        stale = self._note(
            text="stale text",
            linked_record_ids=(),
            updated_at=datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
        )
        self.store.upsert_context_note(stale)

        row = self.connection.execute(
            """
            SELECT note_kind, note_text
            FROM context_notes
            WHERE record_id = 'note-1'
            """
        ).fetchone()
        self.assertEqual(("injury", "Left calf tight after strides."), tuple(row))
        links = self.connection.execute(
            """
            SELECT linked_record_id
            FROM context_note_links
            WHERE note_id = 'note-1'
            """
        ).fetchall()
        self.assertEqual(["activity-1"], [row["linked_record_id"] for row in links])
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT COUNT(*) FROM context_note_links"
            ).fetchone()[0],
        )

    def test_context_note_link_requires_existing_record(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert_context_note(
                self._note(
                    text="Race context",
                    kind=NoteKind.RACE,
                    linked_record_ids=("missing-record",),
                )
            )

    def test_weather_enrichment_retains_raw_links_activity_and_is_idempotent(
        self,
    ) -> None:
        adapter = FixtureWeatherAdapter(
            WeatherFetchResult(
                external_id="weather-activity-1",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                metrics=(
                    temperature_celsius(7.5, quality=0.8),
                    MetricValue("humidity", Measurement(86, Unit.PERCENT)),
                ),
                condition="cloudy",
                raw_content=b'{"temperature":7.5,"condition":"cloudy"}',
                source_updated_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
            )
        )
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection,
            self.store,
            self.activity,
            sync_run_id="run-weather-1",
        )
        enricher.enrich_activity(
            self.connection,
            self.store,
            self.activity,
            sync_run_id="run-weather-1",
        )

        self.assertEqual(
            WeatherRequest(
                activity_id="activity-1",
                started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                timezone="America/Toronto",
            ),
            adapter.requests[0],
        )
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT COUNT(*) FROM weather_observations"
            ).fetchone()[0],
        )
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT COUNT(*) FROM activity_weather_observations"
            ).fetchone()[0],
        )
        lineage = self.connection.execute(
            """
            SELECT reference.source, reference.external_id, raw.storage_path
            FROM source_references AS reference
            JOIN raw_source_records AS raw
              ON raw.raw_record_id = reference.raw_record_id
            WHERE reference.record_id = 'weather:fixture_weather:weather-activity-1'
            """
        ).fetchone()
        self.assertEqual(
            ("fixture_weather", "weather-activity-1"),
            (lineage["source"], lineage["external_id"]),
        )
        self.assertTrue(Path(lineage["storage_path"]).exists())
        temperature = self.connection.execute(
            """
            SELECT metric_status, metric_value, unit, quality
            FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:weather-activity-1'
              AND metric_key = 'temperature'
            """
        ).fetchone()
        self.assertEqual(("measured", 7.5, "degC", 0.8), tuple(temperature))

    def test_missing_weather_is_persisted_as_unavailable(self) -> None:
        adapter = FixtureWeatherAdapter(
            WeatherFetchResult.unavailable(
                external_id="weather-missing-activity-1",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                reason="no_station_observation",
                raw_content=b'{"status":"unavailable"}',
            )
        )
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection,
            self.store,
            self.activity,
            sync_run_id="run-weather-1",
        )

        row = self.connection.execute(
            """
            SELECT metric_status, metric_value, unit, attributes_json
            FROM metric_values
            WHERE owner_record_id =
                  'weather:fixture_weather:weather-missing-activity-1'
              AND metric_key = 'weather'
            """
        ).fetchone()
        self.assertEqual("unavailable", row["metric_status"])
        self.assertIsNone(row["metric_value"])
        self.assertIsNone(row["unit"])
        self.assertIn("no_station_observation", row["attributes_json"])

    def test_weather_adapter_failure_is_persisted_as_unavailable(self) -> None:
        adapter = FailingWeatherAdapter()
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        observation = enricher.enrich_activity(
            self.connection,
            self.store,
            self.activity,
            sync_run_id="run-weather-1",
        )

        self.assertEqual(
            WeatherRequest(
                activity_id="activity-1",
                started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                timezone="America/Toronto",
            ),
            adapter.requests[0],
        )
        self.assertEqual(
            "weather:fixture_weather:activity-1:weather",
            observation.metadata.record_id,
        )
        row = self.connection.execute(
            """
            SELECT metric_status, metric_value, unit, attributes_json
            FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:activity-1:weather'
              AND metric_key = 'weather'
            """
        ).fetchone()
        self.assertEqual("unavailable", row["metric_status"])
        self.assertIsNone(row["metric_value"])
        self.assertIsNone(row["unit"])
        self.assertIn("adapter_error", row["attributes_json"])
        raw = self.connection.execute(
            """
            SELECT raw.storage_path, raw.content_type
            FROM source_references AS reference
            JOIN raw_source_records AS raw
              ON raw.raw_record_id = reference.raw_record_id
            WHERE reference.record_id = 'weather:fixture_weather:activity-1:weather'
            """
        ).fetchone()
        self.assertEqual("application/json", raw["content_type"])
        self.assertIn("adapter_error", Path(raw["storage_path"]).read_text())
        self.assertEqual(
            1,
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM activity_weather_observations
                WHERE activity_id = 'activity-1'
                  AND weather_observation_id =
                      'weather:fixture_weather:activity-1:weather'
                """
            ).fetchone()[0],
        )

    def test_weather_metric_quality_prevents_unavailable_overwrite(self) -> None:
        measured = self._manual_weather(
            record_id="weather-manual-1",
            metrics=(temperature_celsius(9.0, quality=0.9),),
            updated_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        )
        unavailable = self._manual_weather(
            record_id="weather-manual-1",
            metrics=(
                MetricValue(
                    "temperature",
                    None,
                    status=MetricStatus.UNAVAILABLE,
                ),
            ),
            updated_at=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        )

        self.store.upsert_weather_observation(measured, activity_id="activity-1")
        self.store.upsert_weather_observation(unavailable, activity_id="activity-1")

        row = self.connection.execute(
            """
            SELECT metric_status, metric_value, quality
            FROM metric_values
            WHERE owner_record_id = 'weather-manual-1'
              AND metric_key = 'temperature'
            """
        ).fetchone()
        self.assertEqual(("measured", 9.0, 0.9), tuple(row))

    def test_dst_fallback_weather_observations_remain_distinct(self) -> None:
        toronto = ZoneInfo("America/Toronto")
        first = self._manual_weather(
            record_id="weather-first",
            observed_at=datetime(2026, 11, 1, 1, 30, tzinfo=toronto, fold=0),
            metrics=(temperature_celsius(8.0),),
        )
        second = self._manual_weather(
            record_id="weather-second",
            observed_at=datetime(2026, 11, 1, 1, 30, tzinfo=toronto, fold=1),
            metrics=(temperature_celsius(7.0),),
        )

        self.store.upsert_weather_observation(first, activity_id="activity-1")
        self.store.upsert_weather_observation(second, activity_id="activity-1")

        instants = [
            row["observed_at"]
            for row in self.connection.execute(
                "SELECT observed_at FROM weather_observations ORDER BY observed_at"
            )
        ]
        self.assertEqual(
            [
                "2026-11-01T05:30:00+00:00",
                "2026-11-01T06:30:00+00:00",
            ],
            instants,
        )

    def _activity(self) -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id="activity-1",
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id="activity-1",
                        synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                        raw_reference="raw-activity-1",
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
            distance=Measurement(10000, Unit.METRE),
        )

    def _note(
        self,
        *,
        text: str,
        kind: NoteKind = NoteKind.INJURY,
        linked_record_ids: tuple[str, ...] = (),
        updated_at: datetime | None = None,
    ) -> ContextNote:
        timestamp = updated_at or datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
        return ContextNote(
            metadata=RecordMetadata(
                record_id="note-1",
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=timestamp,
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            occurred_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
            kind=kind,
            text=text,
            linked_record_ids=linked_record_ids,
        )

    def _manual_weather(
        self,
        *,
        record_id: str,
        metrics: tuple[MetricValue, ...],
        observed_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> WeatherObservation:
        timestamp = updated_at or datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
        return WeatherObservation(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=timestamp,
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            observed_at=observed_at or datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=metrics,
        )


class WeatherEnrichmentIdempotencyTests(unittest.TestCase):
    """Test idempotency and state transitions during weather enrichment."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self._insert_sync_runs()
        self._insert_raw_records()
        self.store = NormalizationStore(self.connection)
        self.raw_store = RawArtifactStore(root / "raw")

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def _insert_sync_runs(self) -> None:
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-weather-1', 'fixture_weather', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-activity-1', 'fixture', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.commit()

    def _insert_raw_records(self) -> None:
        self.connection.execute(
            """
            INSERT INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, payload, ingested_at
            ) VALUES (
                'raw-activity-1', 'run-activity-1', 'fixture', 'activity-1',
                'activity_file', 'application/octet-stream', 'sha256:activity',
                X'00', '2026-11-01T07:00:00+00:00'
            )
            """
        )
        self.connection.commit()

    def test_second_enrichment_with_same_adapter_does_not_duplicate(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)
        adapter = FixtureWeatherAdapter(
            WeatherFetchResult(
                external_id="weather-activity-1",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                metrics=(temperature_celsius(7.5, quality=0.8),),
                raw_content=b'{"temperature":7.5}',
            )
        )
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )
        enricher.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        count = self.connection.execute(
            "SELECT COUNT(*) FROM weather_observations"
        ).fetchone()[0]
        self.assertEqual(1, count)
        link_count = self.connection.execute(
            "SELECT COUNT(*) FROM activity_weather_observations"
        ).fetchone()[0]
        self.assertEqual(1, link_count)

    def test_higher_quality_observation_overwrites_lower_quality(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        low_quality = WeatherFetchResult(
            external_id="weather-activity-1",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(temperature_celsius(8.0, quality=0.5),),
            raw_content=b'{"temperature":8.0,"quality":"low"}',
        )
        high_quality = WeatherFetchResult(
            external_id="weather-activity-1",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(temperature_celsius(7.5, quality=0.95),),
            raw_content=b'{"temperature":7.5,"quality":"high"}',
        )

        adapter1 = FixtureWeatherAdapter(low_quality)
        enricher1 = WeatherEnricher(
            adapter=adapter1,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )
        enricher1.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        adapter2 = FixtureWeatherAdapter(high_quality)
        enricher2 = WeatherEnricher(
            adapter=adapter2,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        )
        enricher2.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        row = self.connection.execute(
            """
            SELECT metric_value, quality
            FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:weather-activity-1'
              AND metric_key = 'temperature'
            """
        ).fetchone()
        self.assertEqual((7.5, 0.95), (row["metric_value"], row["quality"]))

    def test_lower_quality_observation_does_not_overwrite_higher_quality(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        high_quality = WeatherFetchResult(
            external_id="weather-activity-1",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(temperature_celsius(7.5, quality=0.95),),
            raw_content=b'{"temperature":7.5,"quality":"high"}',
        )
        low_quality = WeatherFetchResult(
            external_id="weather-activity-1",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(temperature_celsius(8.0, quality=0.3),),
            raw_content=b'{"temperature":8.0,"quality":"low"}',
        )

        adapter1 = FixtureWeatherAdapter(high_quality)
        enricher1 = WeatherEnricher(
            adapter=adapter1,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )
        enricher1.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        adapter2 = FixtureWeatherAdapter(low_quality)
        enricher2 = WeatherEnricher(
            adapter=adapter2,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        )
        enricher2.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        row = self.connection.execute(
            """
            SELECT metric_value, quality
            FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:weather-activity-1'
              AND metric_key = 'temperature'
            """
        ).fetchone()
        self.assertEqual((7.5, 0.95), (row["metric_value"], row["quality"]))

    def test_offline_flag_skips_enrichment(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)
        adapter = SlowWeatherAdapter()
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection,
            self.store,
            activity,
            sync_run_id="run-weather-1",
            offline_mode=True,
        )

        self.assertEqual(0, len(adapter.requests))
        count = self.connection.execute(
            "SELECT COUNT(*) FROM weather_observations"
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_unavailable_weather_can_be_overwritten_by_measured(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        unavailable = WeatherFetchResult.unavailable(
            external_id="weather-activity-1",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            reason="no_station_observation",
            raw_content=b'{"status":"unavailable"}',
        )
        measured = WeatherFetchResult(
            external_id="weather-activity-1",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(temperature_celsius(7.5, quality=0.9),),
            raw_content=b'{"temperature":7.5}',
        )

        adapter1 = FixtureWeatherAdapter(unavailable)
        enricher1 = WeatherEnricher(
            adapter=adapter1,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )
        enricher1.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        adapter2 = FixtureWeatherAdapter(measured)
        enricher2 = WeatherEnricher(
            adapter=adapter2,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        )
        enricher2.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        row = self.connection.execute(
            """
            SELECT metric_status, metric_value
            FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:weather-activity-1'
              AND metric_key = 'temperature'
            """
        ).fetchone()
        self.assertEqual(("measured", 7.5), (row["metric_status"], row["metric_value"]))

    def _activity(self, activity_id: str = "activity-1") -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id=activity_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id=activity_id,
                        synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                        raw_reference="raw-activity-1",
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
            distance=Measurement(10000, Unit.METRE),
        )


class WeatherEnrichmentBoundaryTests(unittest.TestCase):
    """Test boundary values and edge cases."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self._insert_sync_runs()
        self._insert_raw_records()
        self.store = NormalizationStore(self.connection)
        self.raw_store = RawArtifactStore(root / "raw")

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def _insert_sync_runs(self) -> None:
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-weather-1', 'fixture_weather', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-activity-1', 'fixture', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.commit()

    def _insert_raw_records(self) -> None:
        self.connection.execute(
            """
            INSERT INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, payload, ingested_at
            ) VALUES (
                'raw-activity-1', 'run-activity-1', 'fixture', 'activity-1',
                'activity_file', 'application/octet-stream', 'sha256:activity',
                X'00', '2026-11-01T07:00:00+00:00'
            )
            """
        )
        self.connection.commit()

    def test_observation_at_utc_midnight(self) -> None:
        activity = Activity(
            metadata=RecordMetadata(
                record_id="activity-midnight",
                timezone="UTC",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id="activity-midnight",
                        synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                        raw_reference="raw-activity-1",
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
            duration=Measurement(1800, Unit.SECOND),
        )
        self.store.upsert_activity(activity)

        adapter = FixtureWeatherAdapter(
            WeatherFetchResult(
                external_id="weather-midnight",
                observed_at=datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
                metrics=(temperature_celsius(10.0),),
                raw_content=b'{"temperature":10.0}',
            )
        )
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 2, 1, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        row = self.connection.execute(
            """
            SELECT observed_at
            FROM weather_observations
            WHERE record_id = 'weather:fixture_weather:weather-midnight'
            """
        ).fetchone()
        self.assertEqual("2026-11-02T00:00:00+00:00", row["observed_at"])

    def test_extreme_temperature_values(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        temps = [-40.0, -10.5, 0.0, 25.5, 50.0]
        for i, temp in enumerate(temps):
            adapter = FixtureWeatherAdapter(
                WeatherFetchResult(
                    external_id=f"weather-temp-{i}",
                    observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                    metrics=(temperature_celsius(temp),),
                    raw_content=json.dumps({"temperature": temp}).encode("utf-8"),
                )
            )
            enricher = WeatherEnricher(
                adapter=adapter,
                raw_store=self.raw_store,
                clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            )

            enricher.enrich_activity(
                self.connection, self.store, activity, sync_run_id="run-weather-1"
            )

        rows = self.connection.execute(
            """
            SELECT metric_value FROM metric_values
            WHERE owner_record_id LIKE 'weather:fixture_weather:weather-temp-%'
              AND metric_key = 'temperature'
            ORDER BY metric_value
            """
        ).fetchall()

        self.assertEqual(len(temps), len(rows))
        for expected, row in zip(sorted(temps), rows):
            self.assertEqual(expected, row["metric_value"])

    def test_zero_and_large_precipitation_values(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        rain_values = [0.0, 0.1, 5.5, 100.0, 250.5]
        for i, rain in enumerate(rain_values):
            adapter = FixtureWeatherAdapter(
                WeatherFetchResult(
                    external_id=f"weather-rain-{i}",
                    observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                    metrics=(
                        MetricValue("precipitation", Measurement(rain, Unit.MILLIMETRE)),
                    ),
                    raw_content=json.dumps({"precipitation": rain}).encode("utf-8"),
                )
            )
            enricher = WeatherEnricher(
                adapter=adapter,
                raw_store=self.raw_store,
                clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            )

            enricher.enrich_activity(
                self.connection, self.store, activity, sync_run_id="run-weather-1"
            )

        rows = self.connection.execute(
            """
            SELECT metric_value FROM metric_values
            WHERE owner_record_id LIKE 'weather:fixture_weather:weather-rain-%'
              AND metric_key = 'precipitation'
            ORDER BY metric_value
            """
        ).fetchall()

        self.assertEqual(len(rain_values), len(rows))
        for expected, row in zip(sorted(rain_values), rows):
            self.assertEqual(expected, row["metric_value"])

    def test_single_metric_in_result(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        adapter = FixtureWeatherAdapter(
            WeatherFetchResult(
                external_id="weather-single-metric",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                metrics=(temperature_celsius(15.0),),
                raw_content=b'{"temperature":15.0}',
            )
        )
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        count = self.connection.execute(
            """
            SELECT COUNT(*) FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:weather-single-metric'
            """
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_very_old_activity_2010(self) -> None:
        old_activity = Activity(
            metadata=RecordMetadata(
                record_id="activity-old",
                timezone="UTC",
                created_at=datetime(2010, 6, 15, 10, 0, tzinfo=UTC),
                updated_at=datetime(2010, 6, 15, 10, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id="activity-old",
                        synced_at=datetime(2010, 6, 15, 10, 0, tzinfo=UTC),
                        raw_reference="raw-activity-1",
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2010, 6, 15, 8, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
        )
        self.store.upsert_activity(old_activity)

        adapter = FixtureWeatherAdapter(
            WeatherFetchResult(
                external_id="weather-2010",
                observed_at=datetime(2010, 6, 15, 8, 30, tzinfo=UTC),
                metrics=(temperature_celsius(22.0),),
                raw_content=b'{"temperature":22.0,"year":2010}',
            )
        )
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        enricher.enrich_activity(
            self.connection, self.store, old_activity, sync_run_id="run-weather-1"
        )

        row = self.connection.execute(
            """
            SELECT metric_value FROM metric_values
            WHERE owner_record_id = 'weather:fixture_weather:weather-2010'
              AND metric_key = 'temperature'
            """
        ).fetchone()
        self.assertEqual(22.0, row["metric_value"])

    def _activity(self, activity_id: str = "activity-1") -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id=activity_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id=activity_id,
                        synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                        raw_reference="raw-activity-1",
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
            distance=Measurement(10000, Unit.METRE),
        )


class WeatherEnrichmentErrorTests(unittest.TestCase):
    """Test error handling and edge cases."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.connection = connect_database(root / "training.sqlite3")
        apply_migrations(self.connection)
        self._insert_sync_runs()
        self._insert_raw_records()
        self.store = NormalizationStore(self.connection)
        self.raw_store = RawArtifactStore(root / "raw")

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def _insert_sync_runs(self) -> None:
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-weather-1', 'fixture_weather', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, source, status, started_at, finished_at
            ) VALUES (
                'run-activity-1', 'fixture', 'completed',
                '2026-11-01T07:00:00+00:00',
                '2026-11-01T07:01:00+00:00'
            )
            """
        )
        self.connection.commit()

    def _insert_raw_records(self) -> None:
        self.connection.execute(
            """
            INSERT INTO raw_source_records (
                raw_record_id, sync_run_id, source, external_id, record_kind,
                content_type, checksum, payload, ingested_at
            ) VALUES (
                'raw-activity-1', 'run-activity-1', 'fixture', 'activity-1',
                'activity_file', 'application/octet-stream', 'sha256:activity',
                X'00', '2026-11-01T07:00:00+00:00'
            )
            """
        )
        self.connection.commit()

    def test_adapter_network_error_persisted_as_unavailable(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        adapter = FailingWeatherAdapter()
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        observation = enricher.enrich_activity(
            self.connection, self.store, activity, sync_run_id="run-weather-1"
        )

        self.assertEqual(1, len(adapter.requests))
        row = self.connection.execute(
            """
            SELECT metric_status
            FROM metric_values
            WHERE owner_record_id = ?
              AND metric_key = 'weather'
            """,
            (observation.metadata.record_id,),
        ).fetchone()
        self.assertEqual("unavailable", row["metric_status"])

    def test_empty_metrics_tuple_raises_validation_error(self) -> None:
        with self.assertRaises(ValueError):
            WeatherFetchResult(
                external_id="weather-empty",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                metrics=(),
            )

    def test_blank_external_id_raises_validation_error(self) -> None:
        with self.assertRaises(ValueError):
            WeatherFetchResult(
                external_id="",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                metrics=(temperature_celsius(10.0),),
            )

    def test_naive_datetime_raises_validation_error(self) -> None:
        with self.assertRaises(ValueError):
            WeatherFetchResult(
                external_id="weather-naive",
                observed_at=datetime(2026, 11, 1, 5, 30),
                metrics=(temperature_celsius(10.0),),
            )

    def test_blank_content_type_raises_validation_error(self) -> None:
        with self.assertRaises(ValueError):
            WeatherFetchResult(
                external_id="weather-blank-ct",
                observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                metrics=(temperature_celsius(10.0),),
                content_type="",
            )

    def test_blank_adapter_source_raises_error(self) -> None:
        activity = self._activity()
        self.store.upsert_activity(activity)

        adapter = BlankSourceWeatherAdapter()
        enricher = WeatherEnricher(
            adapter=adapter,
            raw_store=self.raw_store,
            clock=lambda: datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )

        with self.assertRaises(ValueError):
            enricher.enrich_activity(
                self.connection, self.store, activity, sync_run_id="run-weather-1"
            )

    def _activity(self, activity_id: str = "activity-1") -> Activity:
        return Activity(
            metadata=RecordMetadata(
                record_id=activity_id,
                timezone="America/Toronto",
                created_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                source_references=(
                    SourceReference(
                        source="fixture",
                        external_id=activity_id,
                        synced_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
                        raw_reference="raw-activity-1",
                    ),
                ),
                provenance=Provenance(ProvenanceKind.IMPORTED),
            ),
            activity_type=ActivityType.RUN,
            started_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            duration=Measurement(3600, Unit.SECOND),
            distance=Measurement(10000, Unit.METRE),
        )


class FixtureWeatherAdapter:
    source = "fixture_weather"

    def __init__(self, result: WeatherFetchResult) -> None:
        self._result = result
        self.requests: list[WeatherRequest] = []

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        return self._result


class FailingWeatherAdapter:
    source = "fixture_weather"

    def __init__(self) -> None:
        self.requests: list[WeatherRequest] = []

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        raise RuntimeError("token=private-weather-fixture")


class SlowWeatherAdapter:
    """Adapter that simulates slow network or timeout conditions."""

    source = "fixture_weather"

    def __init__(self, delay_seconds: float = 0.1) -> None:
        self.requests: list[WeatherRequest] = []
        self.delay_seconds = delay_seconds

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        sleep(self.delay_seconds)
        return WeatherFetchResult(
            external_id=request.activity_id,
            observed_at=request.started_at,
            metrics=(temperature_celsius(15.0),),
            raw_content=b'{"temperature":15.0}',
        )


class TimeoutWeatherAdapter:
    """Adapter that raises a timeout error."""

    source = "fixture_weather"

    def __init__(self) -> None:
        self.requests: list[WeatherRequest] = []

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        raise TimeoutError("weather service timeout")


class OfflineWeatherAdapter:
    """Adapter that simulates offline mode (no network available)."""

    source = "fixture_offline_weather"

    def __init__(self) -> None:
        self.requests: list[WeatherRequest] = []

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        raise ConnectionError("offline: no network available")


class BlankSourceWeatherAdapter:
    """Adapter with blank source property (for error testing)."""

    source = ""

    def __init__(self) -> None:
        self.requests: list[WeatherRequest] = []

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        return WeatherFetchResult(
            external_id=request.activity_id,
            observed_at=request.started_at,
            metrics=(temperature_celsius(10.0),),
            raw_content=b'{"temperature":10.0}',
        )


class MultiMetricWeatherAdapter:
    """Adapter that returns weather with multiple metrics."""

    source = "fixture_multi_weather"

    def __init__(self, result: WeatherFetchResult | None = None) -> None:
        self.requests: list[WeatherRequest] = []
        self._result = result or WeatherFetchResult(
            external_id="weather-multi",
            observed_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            metrics=(
                temperature_celsius(15.5, quality=0.9),
                MetricValue(
                    "wind_speed", Measurement(8.5, Unit.METRE_PER_SECOND), quality=0.85
                ),
                MetricValue(
                    "precipitation", Measurement(2.5, Unit.MILLIMETRE), quality=0.8
                ),
                MetricValue("humidity", Measurement(72, Unit.PERCENT), quality=0.9),
            ),
            condition="partly_cloudy",
            raw_content=b'{"temperature":15.5,"wind":8.5,"rain":2.5,"humidity":72}',
        )

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        return self._result


class MalformedResponseWeatherAdapter:
    """Adapter that returns malformed weather data."""

    source = "fixture_malformed_weather"

    def __init__(self) -> None:
        self.requests: list[WeatherRequest] = []

    def fetch(self, request: WeatherRequest) -> WeatherFetchResult:
        self.requests.append(request)
        raise ValueError("malformed JSON from weather API")


if __name__ == "__main__":
    unittest.main()
