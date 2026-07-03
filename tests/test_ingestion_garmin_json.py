"""Tests for Garmin account-export JSON health importer (issue 20)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.ingestion import RawArtifactStore, SyncRunner, SyncStatus
from trainingos.ingestion.garmin_json import (
    GARMIN_JSON_PARSER,
    GARMIN_JSON_SOURCE,
    GarminJsonHealthAdapter,
    GarminJsonHealthHandler,
    GarminJsonPayload,
    parse_garmin_daily_summary_json,
    parse_garmin_sleep_json,
)
from trainingos.storage import apply_migrations, connect_database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SLEEP_PAYLOAD_FULL: dict = {
    "dailySleepDTO": {
        "calendarDate": "2026-06-15",
        "sleepTimeSeconds": 25380,
        "deepSleepSeconds": 7200,
        "lightSleepSeconds": 8400,
        "remSleepSeconds": 7980,
        "awakeSleepSeconds": 1800,
        "averageHRV": 52,
        "averageSpO2Value": 96.5,
        "lowestSpO2Value": 93.0,
    }
}

SUMMARY_PAYLOAD_FULL: dict = {
    "calendarDate": "2026-06-15",
    "restingHeartRate": 48,
    "averageStressLevel": 32,
    "maxStressLevel": 67,
    "bodyBatteryHighestValue": 85,
    "bodyBatteryLowestValue": 43,
    "vo2Max": 52.0,
}


def _sleep_bytes(records: list[dict] | None = None) -> bytes:
    return json.dumps(records if records is not None else [SLEEP_PAYLOAD_FULL]).encode()


def _summary_bytes(records: list[dict] | None = None) -> bytes:
    return json.dumps(records if records is not None else [SUMMARY_PAYLOAD_FULL]).encode()


def _clock(ts: str = "2026-06-16T12:00:00+00:00"):
    dt = datetime.fromisoformat(ts)
    return lambda: dt


# ---------------------------------------------------------------------------
# A + B + C + D + E  — parse_garmin_sleep_json (pure unit tests, no DB)
# ---------------------------------------------------------------------------

class GarminSleepParserTests(unittest.TestCase):

    def _parse(self, records: list[dict], *, raw_record_id: str = "raw-1") -> list:
        return parse_garmin_sleep_json(
            records,
            raw_record_id=raw_record_id,
            synced_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
            timezone="America/New_York",
        )

    # A — happy path

    def test_parse_full_sleep_payload_returns_one_daily_health_for_date(self) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL])

        self.assertEqual(1, len(results))
        self.assertEqual(date(2026, 6, 15), results[0].local_date)
        self.assertEqual("America/New_York", results[0].metadata.timezone)

    def test_parse_full_sleep_payload_includes_all_seven_metrics(self) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL])
        keys = {m.key for m in results[0].metrics}

        self.assertIn("sleep_duration", keys)
        self.assertIn("sleep_deep", keys)
        self.assertIn("sleep_light", keys)
        self.assertIn("sleep_rem", keys)
        self.assertIn("sleep_awake", keys)
        self.assertIn("hrv_overnight", keys)
        self.assertIn("spo2_average", keys)

    def test_parse_sleep_duration_stored_in_seconds(self) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL])
        metric = {m.key: m for m in results[0].metrics}["sleep_duration"]

        self.assertEqual(25380.0, metric.measurement.value)
        self.assertEqual("s", metric.measurement.unit.value)

    def test_parse_hrv_overnight_stored_in_milliseconds(self) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL])
        metric = {m.key: m for m in results[0].metrics}["hrv_overnight"]

        self.assertEqual(52.0, metric.measurement.value)
        self.assertEqual("ms", metric.measurement.unit.value)

    def test_parse_spo2_stored_in_percent(self) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL])
        metric = {m.key: m for m in results[0].metrics}["spo2_average"]

        self.assertEqual(96.5, metric.measurement.value)
        self.assertEqual("%", metric.measurement.unit.value)

    def test_parse_source_reference_carries_parser_name_and_version(self) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL])
        ref = results[0].metadata.source_references[0]

        self.assertEqual(GARMIN_JSON_SOURCE, ref.source)
        self.assertEqual(GARMIN_JSON_PARSER.name, ref.parser.name)
        self.assertEqual(GARMIN_JSON_PARSER.version, ref.parser.version)

    def test_parse_source_reference_raw_reference_matches_supplied_raw_record_id(
        self,
    ) -> None:
        results = self._parse([SLEEP_PAYLOAD_FULL], raw_record_id="raw-test-42")
        ref = results[0].metadata.source_references[0]

        self.assertEqual("raw-test-42", ref.raw_reference)

    def test_parse_multi_day_sleep_file_returns_one_result_per_date(self) -> None:
        records = [
            {
                "dailySleepDTO": {
                    "calendarDate": f"2026-06-{day:02d}",
                    "sleepTimeSeconds": 25000 + day,
                }
            }
            for day in range(1, 31)
        ]

        results = self._parse(records)

        self.assertEqual(30, len(results))
        dates = [r.local_date for r in results]
        self.assertEqual(sorted(dates), dates)

    # B — boundary values

    def test_parse_sleep_payload_with_zero_seconds_returns_zero_measurements(
        self,
    ) -> None:
        payload = {
            "dailySleepDTO": {
                "calendarDate": "2026-06-15",
                "sleepTimeSeconds": 0,
                "deepSleepSeconds": 0,
                "lightSleepSeconds": 0,
                "remSleepSeconds": 0,
                "awakeSleepSeconds": 0,
            }
        }

        results = self._parse([payload])
        durations = {m.key: m.measurement.value for m in results[0].metrics}

        self.assertEqual(0.0, durations["sleep_duration"])

    def test_parse_spo2_at_100_percent_is_not_clamped(self) -> None:
        payload = {
            "dailySleepDTO": {
                "calendarDate": "2026-06-15",
                "sleepTimeSeconds": 3600,
                "averageSpO2Value": 100.0,
            }
        }

        results = self._parse([payload])
        metric = {m.key: m for m in results[0].metrics}["spo2_average"]

        self.assertEqual(100.0, metric.measurement.value)

    # C — equivalence partitioning (partial vs. full payloads)

    def test_parse_sleep_payload_with_only_duration_returns_only_duration_metric(
        self,
    ) -> None:
        payload = {
            "dailySleepDTO": {
                "calendarDate": "2026-06-15",
                "sleepTimeSeconds": 21600,
            }
        }

        results = self._parse([payload])
        keys = {m.key for m in results[0].metrics}

        self.assertIn("sleep_duration", keys)
        self.assertNotIn("hrv_overnight", keys)
        self.assertNotIn("spo2_average", keys)

    def test_parse_sleep_payload_with_null_hrv_omits_hrv_metric(self) -> None:
        payload = {
            "dailySleepDTO": {
                "calendarDate": "2026-06-15",
                "sleepTimeSeconds": 21600,
                "averageHRV": None,
            }
        }

        results = self._parse([payload])
        keys = {m.key for m in results[0].metrics}

        self.assertNotIn("hrv_overnight", keys)

    # D — edge cases

    def test_parse_empty_list_returns_empty(self) -> None:
        self.assertEqual([], self._parse([]))

    def test_parse_single_date_list_returns_one_result(self) -> None:
        self.assertEqual(1, len(self._parse([SLEEP_PAYLOAD_FULL])))

    # E — error / negative tests

    def test_parse_invalid_calendar_date_raises_value_error(self) -> None:
        payload = {
            "dailySleepDTO": {
                "calendarDate": "not-a-date",
                "sleepTimeSeconds": 3600,
            }
        }

        with self.assertRaises(ValueError) as ctx:
            self._parse([payload])

        self.assertIn("calendarDate", str(ctx.exception))

    def test_parse_missing_daily_sleep_dto_raises_value_error(self) -> None:
        with self.assertRaises((ValueError, KeyError)):
            self._parse([{"someOtherKey": {}}])

    def test_parse_non_finite_metric_value_raises_value_error(self) -> None:
        payload = {
            "dailySleepDTO": {
                "calendarDate": "2026-06-15",
                "sleepTimeSeconds": float("inf"),
            }
        }

        with self.assertRaises(ValueError):
            self._parse([payload])


# ---------------------------------------------------------------------------
# parse_garmin_daily_summary_json (pure unit tests, no DB)
# ---------------------------------------------------------------------------

class GarminDailySummaryParserTests(unittest.TestCase):

    def _parse(self, records: list[dict], *, raw_record_id: str = "raw-1") -> list:
        return parse_garmin_daily_summary_json(
            records,
            raw_record_id=raw_record_id,
            synced_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
            timezone="America/New_York",
        )

    # A — happy path

    def test_parse_full_summary_returns_one_daily_health_for_date(self) -> None:
        results = self._parse([SUMMARY_PAYLOAD_FULL])

        self.assertEqual(1, len(results))
        self.assertEqual(date(2026, 6, 15), results[0].local_date)

    def test_parse_full_summary_includes_hr_stress_battery_vo2_metrics(self) -> None:
        results = self._parse([SUMMARY_PAYLOAD_FULL])
        keys = {m.key for m in results[0].metrics}

        self.assertIn("resting_heart_rate", keys)
        self.assertIn("stress_average", keys)
        self.assertIn("stress_max", keys)
        self.assertIn("body_battery_high", keys)
        self.assertIn("body_battery_low", keys)
        self.assertIn("vo2_max", keys)

    def test_parse_resting_heart_rate_stored_in_bpm(self) -> None:
        results = self._parse([SUMMARY_PAYLOAD_FULL])
        metric = {m.key: m for m in results[0].metrics}["resting_heart_rate"]

        self.assertEqual(48.0, metric.measurement.value)
        self.assertEqual("bpm", metric.measurement.unit.value)

    def test_parse_vo2max_stored_in_ml_kg_min(self) -> None:
        results = self._parse([SUMMARY_PAYLOAD_FULL])
        metric = {m.key: m for m in results[0].metrics}["vo2_max"]

        self.assertEqual(52.0, metric.measurement.value)
        self.assertEqual("mL/kg/min", metric.measurement.unit.value)

    def test_parse_stress_metrics_stored_in_count_unit(self) -> None:
        results = self._parse([SUMMARY_PAYLOAD_FULL])
        metrics = {m.key: m for m in results[0].metrics}

        self.assertEqual("count", metrics["stress_average"].measurement.unit.value)
        self.assertEqual(32.0, metrics["stress_average"].measurement.value)
        self.assertEqual(67.0, metrics["stress_max"].measurement.value)

    # C — equivalence partitioning

    def test_parse_null_vo2max_omits_vo2_metric(self) -> None:
        payload = {**SUMMARY_PAYLOAD_FULL, "vo2Max": None}

        results = self._parse([payload])
        keys = {m.key for m in results[0].metrics}

        self.assertNotIn("vo2_max", keys)

    def test_parse_absent_vo2max_omits_vo2_metric(self) -> None:
        payload = {k: v for k, v in SUMMARY_PAYLOAD_FULL.items() if k != "vo2Max"}

        results = self._parse([payload])
        keys = {m.key for m in results[0].metrics}

        self.assertNotIn("vo2_max", keys)

    def test_parse_absent_body_battery_omits_battery_metrics(self) -> None:
        payload = {
            "calendarDate": "2026-06-15",
            "restingHeartRate": 48,
            "averageStressLevel": 32,
        }

        results = self._parse([payload])
        keys = {m.key for m in results[0].metrics}

        self.assertNotIn("body_battery_high", keys)
        self.assertNotIn("body_battery_low", keys)

    def test_parse_zero_stress_returns_zero_metric(self) -> None:
        payload = {
            "calendarDate": "2026-06-15",
            "averageStressLevel": 0,
            "maxStressLevel": 0,
        }

        results = self._parse([payload])
        metrics = {m.key: m for m in results[0].metrics}

        self.assertEqual(0.0, metrics["stress_average"].measurement.value)

    # D — edge cases

    def test_parse_empty_list_returns_empty(self) -> None:
        self.assertEqual([], self._parse([]))

    # E — error / negative

    def test_parse_invalid_calendar_date_raises_value_error(self) -> None:
        payload = {"calendarDate": "bad", "restingHeartRate": 48}

        with self.assertRaises(ValueError) as ctx:
            self._parse([payload])

        self.assertIn("calendarDate", str(ctx.exception))


# ---------------------------------------------------------------------------
# GarminJsonHealthAdapter — ZIP discovery and PII exclusion
# ---------------------------------------------------------------------------

class GarminJsonHealthAdapterTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _make_zip(self, members: dict[str, bytes]) -> Path:
        zip_path = self.root / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return zip_path

    # A — happy path

    def test_adapter_discovers_sleep_json_from_export_zip(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))
        self.assertTrue(page.done)

    def test_adapter_discovers_daily_summary_json_from_export_zip(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessEpochSummaries_2026-06-15.json": (
                    _summary_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))

    def test_adapter_discovers_both_file_types_from_same_zip(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
                "DI_CONNECT/DI-Connect-Wellness/wellnessEpochSummaries_2026-06-15.json": (
                    _summary_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(2, len(page.records))

    def test_adapter_payload_carries_file_bytes(self) -> None:
        content = _sleep_bytes()
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": content,
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertIsInstance(page.records[0].payload, GarminJsonPayload)
        self.assertEqual(content, page.records[0].payload.content)

    def test_adapter_payload_carries_inferred_file_type(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual("sleep", page.records[0].payload.file_type)

    def test_adapter_discovers_json_from_directory(self) -> None:
        wellness_dir = self.root / "wellness"
        wellness_dir.mkdir()
        (wellness_dir / "wellnessSleepData_2026-06-15.json").write_bytes(_sleep_bytes())
        (wellness_dir / "wellnessEpochSummaries_2026-06-15.json").write_bytes(
            _summary_bytes()
        )

        adapter = GarminJsonHealthAdapter((wellness_dir,))
        page = adapter.fetch(None, 100)

        self.assertEqual(2, len(page.records))

    def test_zip_with_no_wellness_json_yields_zero_records(self) -> None:
        zip_path = self._make_zip(
            {"customer_data/customer.json": b'{"name": "Alice"}'}
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(0, len(page.records))
        self.assertTrue(page.done)

    # D — edge cases

    def test_adapter_from_empty_zip_yields_zero_records(self) -> None:
        zip_path = self._make_zip({})
        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)
        self.assertEqual(0, len(page.records))

    # E — error / negative

    def test_adapter_raises_on_unreadable_zip(self) -> None:
        bad_zip = self.root / "bad.zip"
        bad_zip.write_bytes(b"not a zip")

        with self.assertRaises((ValueError, OSError)):
            GarminJsonHealthAdapter((bad_zip,))

    # G — PII exclusion

    def test_adapter_excludes_customer_json(self) -> None:
        zip_path = self._make_zip(
            {
                "customer_data/customer.json": b'{"name": "Alice"}',
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))

    def test_adapter_excludes_user_profile_json(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-User/user_profile.json": b'{"email": "alice@example.com"}',
                "DI_CONNECT/DI-Connect-User/user_biometrics.json": b'{"height": 170}',
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))

    def test_adapter_excludes_files_with_email_in_name(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/alice@example.com_sleepData.json": (
                    _sleep_bytes()
                ),
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        self.assertEqual(1, len(page.records))

    def test_external_id_never_contains_at_sign(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        adapter = GarminJsonHealthAdapter((zip_path,))
        page = adapter.fetch(None, 100)

        for record in page.records:
            self.assertNotIn("@", record.external_id)


# ---------------------------------------------------------------------------
# Full import pipeline — integration tests (real SQLite + SyncRunner)
# ---------------------------------------------------------------------------

class GarminJsonHealthImportTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "training.sqlite3"
        self.raw_dir = self.root / "raw"
        self.connection = connect_database(self.database_path)
        apply_migrations(self.connection)
        self.clock = _clock()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def _make_zip(self, members: dict[str, bytes]) -> Path:
        zip_path = self.root / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return zip_path

    def _runner(self) -> SyncRunner:
        return SyncRunner(self.connection, clock=self.clock)

    def _handler(
        self,
        timezone: str = "America/New_York",
        clock=None,
    ) -> GarminJsonHealthHandler:
        return GarminJsonHealthHandler(
            RawArtifactStore(self.raw_dir),
            timezone=timezone,
            clock=clock or self.clock,
        )

    def _count(self, table: str) -> int:
        allowed = {"daily_health", "raw_source_records", "metric_values", "source_references"}
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # A — happy path (full pipeline)

    def test_import_sleep_zip_writes_daily_health_and_raw_artifact(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        report = self._runner().run(
            GarminJsonHealthAdapter((zip_path,)),
            self._handler(),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(1, report.imported_count)
        self.assertEqual(1, self._count("daily_health"))
        self.assertEqual(1, self._count("raw_source_records"))

    def test_import_writes_sleep_metrics_to_metric_values(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )

        self._runner().run(
            GarminJsonHealthAdapter((zip_path,)),
            self._handler(),
        )

        keys = {
            row[0]
            for row in self.connection.execute(
                "SELECT metric_key FROM metric_values"
            ).fetchall()
        }
        self.assertIn("sleep_duration", keys)
        self.assertIn("hrv_overnight", keys)

    def test_import_multi_day_json_writes_one_row_per_date(self) -> None:
        records = [
            {
                "dailySleepDTO": {
                    "calendarDate": f"2026-06-{day:02d}",
                    "sleepTimeSeconds": 25000,
                }
            }
            for day in range(1, 6)
        ]
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06.json": (
                    json.dumps(records).encode()
                ),
            }
        )

        report = self._runner().run(
            GarminJsonHealthAdapter((zip_path,)),
            self._handler(),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(5, self._count("daily_health"))

    def test_import_empty_export_completes_with_zero_imported(self) -> None:
        zip_path = self._make_zip({})

        report = self._runner().run(
            GarminJsonHealthAdapter((zip_path,)),
            self._handler(),
        )

        self.assertEqual(SyncStatus.COMPLETED, report.status)
        self.assertEqual(0, report.imported_count)
        self.assertEqual(0, self._count("daily_health"))

    # F — idempotency and state

    def test_second_import_of_same_file_is_fully_skipped(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )
        runner = self._runner()
        handler = self._handler()

        first = runner.run(GarminJsonHealthAdapter((zip_path,)), handler)
        second = runner.run(GarminJsonHealthAdapter((zip_path,)), handler)

        self.assertEqual(SyncStatus.COMPLETED, first.status)
        self.assertEqual(1, first.imported_count)
        self.assertEqual(SyncStatus.COMPLETED, second.status)
        self.assertEqual(0, second.imported_count)
        self.assertEqual(1, second.skipped_count)
        self.assertEqual(1, self._count("daily_health"))
        self.assertEqual(1, self._count("raw_source_records"))

    def test_sleep_then_summary_for_same_date_merges_onto_one_daily_health_row(
        self,
    ) -> None:
        sleep_zip = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )
        summary_zip = self.root / "summary.zip"
        with zipfile.ZipFile(summary_zip, "w") as archive:
            archive.writestr(
                "DI_CONNECT/DI-Connect-Wellness/wellnessEpochSummaries_2026-06-15.json",
                _summary_bytes(),
            )

        clock2 = _clock("2026-06-16T13:00:00+00:00")

        self._runner().run(
            GarminJsonHealthAdapter((sleep_zip,)),
            self._handler(),
        )
        SyncRunner(self.connection, clock=clock2).run(
            GarminJsonHealthAdapter((summary_zip,)),
            self._handler(clock=clock2),
        )

        self.assertEqual(1, self._count("daily_health"))

        keys = {
            row[0]
            for row in self.connection.execute(
                "SELECT metric_key FROM metric_values"
            ).fetchall()
        }
        self.assertIn("sleep_duration", keys)
        self.assertIn("resting_heart_rate", keys)

    def test_re_import_of_summary_does_not_erase_sleep_metrics_from_first_import(
        self,
    ) -> None:
        """Importing a daily-summary file for an already-imported sleep date must
        add HR/stress metrics without removing the previously-stored sleep metrics."""
        sleep_zip = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    _sleep_bytes()
                ),
            }
        )
        summary_zip = self.root / "summary.zip"
        with zipfile.ZipFile(summary_zip, "w") as archive:
            archive.writestr(
                "DI_CONNECT/DI-Connect-Wellness/wellnessEpochSummaries_2026-06-15.json",
                _summary_bytes(),
            )

        clock2 = _clock("2026-06-16T13:00:00+00:00")

        self._runner().run(
            GarminJsonHealthAdapter((sleep_zip,)),
            self._handler(),
        )
        SyncRunner(self.connection, clock=clock2).run(
            GarminJsonHealthAdapter((summary_zip,)),
            self._handler(clock=clock2),
        )

        keys = {
            row[0]
            for row in self.connection.execute(
                "SELECT metric_key FROM metric_values"
            ).fetchall()
        }
        self.assertIn("sleep_duration", keys, "sleep metrics should survive summary re-import")
        self.assertIn("resting_heart_rate", keys, "summary metrics should be added")

    # E — error handling in handler

    def test_handler_raises_sync_error_on_malformed_json_bytes(self) -> None:
        from trainingos.ingestion.sync import SyncError, SyncRecord

        bad_payload = GarminJsonPayload(
            content=b"not json at all",
            file_type="sleep",
            source_path="garmin-json:sha256:badbad",
        )
        record = SyncRecord(
            external_id="garmin-json:sha256:badbad",
            cursor_after="1",
            payload=bad_payload,
        )

        # Manually insert a running sync_run so the handler can find it
        self.connection.execute(
            """
            INSERT INTO sync_runs (sync_run_id, source, status, started_at)
            VALUES ('run-bad', 'garmin_json', 'running', '2026-06-16T12:00:00+00:00')
            """
        )
        self.connection.commit()

        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(SyncError) as ctx:
            self._handler()(self.connection, record)
        self.connection.rollback()

        self.assertTrue(ctx.exception.code)

    def test_full_import_with_malformed_json_file_reports_failure(self) -> None:
        zip_path = self._make_zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json": (
                    b"not json"
                ),
            }
        )

        report = self._runner().run(
            GarminJsonHealthAdapter((zip_path,)),
            self._handler(),
        )

        self.assertEqual(SyncStatus.FAILED, report.status)
        self.assertEqual(1, report.failed_count)
        self.assertEqual(0, self._count("daily_health"))

        error = self.connection.execute(
            "SELECT error_code FROM sync_errors WHERE sync_run_id = ?",
            (report.sync_run_id,),
        ).fetchone()
        self.assertIsNotNone(error)


# ---------------------------------------------------------------------------
# H — Units, timezone, and provenance (integration)
# ---------------------------------------------------------------------------

class GarminJsonUnitsTimezoneTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "training.sqlite3"
        self.raw_dir = self.root / "raw"
        self.connection = connect_database(self.database_path)
        apply_migrations(self.connection)
        self.clock = _clock()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def _run_sleep_import(self, timezone: str = "America/New_York") -> None:
        zip_path = self.root / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr(
                "DI_CONNECT/DI-Connect-Wellness/wellnessSleepData_2026-06-15.json",
                _sleep_bytes(),
            )
        SyncRunner(self.connection, clock=self.clock).run(
            GarminJsonHealthAdapter((zip_path,)),
            GarminJsonHealthHandler(
                RawArtifactStore(self.raw_dir),
                timezone=timezone,
                clock=self.clock,
            ),
        )

    def test_local_date_uses_calendar_date_field_not_utc_timestamp(self) -> None:
        self._run_sleep_import()

        row = self.connection.execute(
            "SELECT local_date FROM daily_health"
        ).fetchone()
        self.assertEqual("2026-06-15", row["local_date"])

    def test_timezone_stored_on_record_matches_configured_timezone(self) -> None:
        self._run_sleep_import(timezone="America/Los_Angeles")

        row = self.connection.execute(
            "SELECT timezone FROM records WHERE record_type = 'daily_health'"
        ).fetchone()
        self.assertEqual("America/Los_Angeles", row["timezone"])

    def test_synced_at_in_source_references_is_utc_isoformat(self) -> None:
        self._run_sleep_import()

        row = self.connection.execute(
            "SELECT synced_at FROM source_references"
        ).fetchone()
        self.assertIsNotNone(row)
        # Must parse as UTC-aware datetime without error
        dt = datetime.fromisoformat(row["synced_at"])
        self.assertIsNotNone(dt.tzinfo)

    def test_raw_source_record_content_type_is_application_json(self) -> None:
        self._run_sleep_import()

        row = self.connection.execute(
            "SELECT content_type FROM raw_source_records WHERE source = ?",
            (GARMIN_JSON_SOURCE,),
        ).fetchone()
        self.assertEqual("application/json", row["content_type"])

    def test_raw_source_record_checksum_is_sha256_prefixed_hex(self) -> None:
        self._run_sleep_import()

        row = self.connection.execute(
            "SELECT checksum FROM raw_source_records WHERE source = ?",
            (GARMIN_JSON_SOURCE,),
        ).fetchone()
        self.assertRegex(row["checksum"], r"^sha256:[0-9a-f]{64}$")

    def test_source_reference_carries_parser_name_and_version(self) -> None:
        self._run_sleep_import()

        row = self.connection.execute(
            "SELECT parser_name, parser_version FROM source_references"
        ).fetchone()
        self.assertEqual(GARMIN_JSON_PARSER.name, row["parser_name"])
        self.assertEqual(GARMIN_JSON_PARSER.version, row["parser_version"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class GarminJsonImportCliTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "trainingos.ingestion.json_import", *args],
            cwd=Path(__file__).resolve().parents[1],
            env=self._pythonpath_env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_rejects_missing_path_without_traceback(self) -> None:
        result = self._run_cli("/path/that/does/not/exist")

        self.assertEqual(2, result.returncode)
        self.assertIn("import path does not exist", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_rejects_unreadable_zip_without_traceback(self) -> None:
        bad_zip = self.root / "bad.zip"
        bad_zip.write_bytes(b"not a zip")

        result = self._run_cli(str(bad_zip))

        self.assertEqual(2, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def _pythonpath_env(self) -> dict[str, str]:
        env = os.environ.copy()
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
        return env


if __name__ == "__main__":
    unittest.main()
