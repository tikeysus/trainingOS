from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trainingos.analytics import derive_training_metrics
from trainingos.blocks import create_block
from trainingos.domain import (
    Activity,
    ActivitySample,
    ActivityType,
    ContextNote,
    Lap,
    Measurement,
    MetricValue,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
    WeatherObservation,
)
from trainingos.normalization import NormalizationStore
from trainingos.retrieval import (
    _training_block_documents,
    generate_retrieval_documents,
    search_retrieval_documents,
)
from trainingos.storage import apply_migrations, connect_database


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.connection = connect_database(
            Path(self.temporary_directory.name) / "training.sqlite3"
        )
        apply_migrations(self.connection)
        self.store = NormalizationStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_generates_all_document_types_with_traceable_evidence(self) -> None:
        self._seed_training_context()

        report = generate_retrieval_documents(
            self.connection,
            now=datetime(2026, 11, 9, 13, 0, tzinfo=UTC),
        )

        self.assertEqual(6, report.generated_count)
        types = self.connection.execute(
            """
            SELECT document_type, COUNT(*) AS count
            FROM retrieval_documents
            GROUP BY document_type
            ORDER BY document_type
            """
        ).fetchall()
        self.assertEqual(
            [
                ("activity", 1),
                ("note", 1),
                ("race", 1),
                ("training_block", 1),
                ("week", 1),
                ("workout", 1),
            ],
            [tuple(row) for row in types],
        )
        week = self._document("week", "weekly_summary:America/Toronto:2026-11-02")
        self.assertIn("weekly_distance", week["body"])
        self.assertIn("weekly_summary", week["evidence_json"])
        self.assertIn("1.0.0", week["body"])
        self.assertIn("recovery coverage is incomplete", week["caveats_json"])
        activity = self._document("activity", "activity-1")
        self.assertIn("Weather:", activity["body"])
        self.assertIn("calf tightness", activity["body"])
        self.assertIn("weather-1", activity["evidence_json"])
        race = self._document("race", "race-1")
        self.assertIn("race result is missing", race["caveats_json"])
        block = self._document("training_block", "block-1")
        self.assertIn("Hamilton Marathon", block["body"])
        self.assertIn("weekly_summary:America/Toronto:2026-11-02", block["body"])

    def test_search_uses_local_fts_and_type_filters(self) -> None:
        self._seed_training_context()
        generate_retrieval_documents(
            self.connection,
            now=datetime(2026, 11, 9, 13, 0, tzinfo=UTC),
        )

        results = search_retrieval_documents(
            self.connection,
            "marathon?",
            document_types=("training_block",),
        )

        self.assertEqual(1, len(results))
        self.assertEqual("training_block", results[0].document.document_type)
        self.assertEqual("block-1", results[0].document.source_record_id)
        self.assertIn("weekly_summary:America/Toronto:2026-11-02", results[0].document.body)

    def test_generation_is_idempotent_and_updates_changed_sources(self) -> None:
        self._seed_training_context()
        now = datetime(2026, 11, 9, 13, 0, tzinfo=UTC)

        first = generate_retrieval_documents(self.connection, now=now)
        first_rows = self._document_rows()
        second = generate_retrieval_documents(self.connection, now=now)
        second_rows = self._document_rows()

        self.assertEqual(first.generated_count, second.generated_count)
        self.assertEqual(first_rows, second_rows)
        self.store.upsert_context_note(
            self._note(
                "note-1",
                "calf tightness resolved after rest",
                updated_at=datetime(2026, 11, 9, 14, 0, tzinfo=UTC),
            )
        )
        generate_retrieval_documents(
            self.connection,
            now=datetime(2026, 11, 9, 15, 0, tzinfo=UTC),
        )

        activity = self._document("activity", "activity-1")
        self.assertIn("resolved after rest", activity["body"])
        self.assertEqual(
            "2026-11-09T14:00:00+00:00",
            activity["source_updated_at"],
        )

    def test_deleted_source_record_is_not_searchable(self) -> None:
        self._seed_training_context()
        generate_retrieval_documents(
            self.connection,
            now=datetime(2026, 11, 9, 13, 0, tzinfo=UTC),
        )

        self.connection.execute("DELETE FROM records WHERE record_id = 'note-1'")
        report = generate_retrieval_documents(
            self.connection,
            now=datetime(2026, 11, 9, 14, 0, tzinfo=UTC),
        )

        self.assertEqual(1, report.deleted_count)
        self.assertIsNone(self._optional_document("note", "note-1"))
        self.assertEqual((), search_retrieval_documents(self.connection, "tightness"))

    def test_invalid_query_inputs_are_rejected(self) -> None:
        self._seed_training_context()
        generate_retrieval_documents(
            self.connection,
            now=datetime(2026, 11, 9, 13, 0, tzinfo=UTC),
        )

        with self.assertRaisesRegex(ValueError, "query"):
            search_retrieval_documents(self.connection, " ")
        with self.assertRaisesRegex(ValueError, "searchable text"):
            search_retrieval_documents(self.connection, "::")
        with self.assertRaisesRegex(ValueError, "unsupported document type"):
            search_retrieval_documents(
                self.connection,
                "marathon",
                document_types=("raw_payload",),
            )
        with self.assertRaisesRegex(ValueError, "limit"):
            search_retrieval_documents(self.connection, "marathon", limit=0)

    # ---- D. Edge Cases ----

    def test_training_block_documents_does_not_raise_after_migration_010(self) -> None:
        from datetime import date

        block_id = create_block(
            self.connection,
            goal="Test Block",
            start_date=date(2026, 11, 2),
            race_date=date(2026, 11, 9),
            now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        )

        docs = list(_training_block_documents(self.connection))
        self.assertEqual(1, len(docs))
        self.assertEqual("training_block", docs[0].document_type)
        self.assertEqual(block_id, docs[0].source_record_id)

    def _seed_training_context(self) -> None:
        self.store.upsert_activity(
            Activity(
                metadata=self._metadata("activity-1"),
                activity_type=ActivityType.RUN,
                started_at=datetime(2026, 11, 2, 11, 0, tzinfo=UTC),
                duration=Measurement(3600, Unit.SECOND),
                distance=Measurement(10000, Unit.METRE),
                title="Steady marathon aerobic run",
            )
        )
        self.store.upsert_lap(
            Lap(
                metadata=self._metadata("lap-1"),
                activity_id="activity-1",
                index=0,
                started_at=datetime(2026, 11, 2, 11, 0, tzinfo=UTC),
                duration=Measurement(270, Unit.SECOND),
                distance=Measurement(1000, Unit.METRE),
            )
        )
        for index, heart_rate in enumerate((100, 100, 110, 110)):
            self.store.upsert_sample(
                ActivitySample(
                    metadata=self._metadata(f"sample-{index}"),
                    activity_id="activity-1",
                    recorded_at=datetime(2026, 11, 2, 11, index, tzinfo=UTC),
                    metrics=(
                        MetricValue(
                            "heart_rate",
                            Measurement(heart_rate, Unit.BEAT_PER_MINUTE),
                        ),
                    ),
                )
            )
        self.store.upsert_context_note(
            self._note("note-1", "Left calf tightness after strides")
        )
        self.store.upsert_weather_observation(
            WeatherObservation(
                metadata=self._metadata("weather-1"),
                observed_at=datetime(2026, 11, 2, 11, 0, tzinfo=UTC),
                metrics=(
                    MetricValue(
                        "temperature",
                        Measurement(7.5, Unit.DEGREE_CELSIUS),
                    ),
                ),
                condition="cloudy",
            ),
            activity_id="activity-1",
        )
        self._race("race-1")
        self._block("block-1", "2026-11-08")
        derive_training_metrics(
            self.connection,
            now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        )

    def _metadata(
        self,
        record_id: str,
        *,
        updated_at: datetime | None = None,
    ) -> RecordMetadata:
        timestamp = updated_at or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        return RecordMetadata(
            record_id=record_id,
            timezone="America/Toronto",
            created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
            updated_at=timestamp,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        )

    def _note(
        self,
        record_id: str,
        text: str,
        *,
        updated_at: datetime | None = None,
    ) -> ContextNote:
        return ContextNote(
            metadata=self._metadata(record_id, updated_at=updated_at),
            occurred_at=datetime(2026, 11, 2, 13, 0, tzinfo=UTC),
            kind=NoteKind.INJURY,
            text=text,
            linked_record_ids=("activity-1",),
        )

    def _race(self, record_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'race', 'America/Toronto', ?, ?, 'user_entered')
            """,
            (
                record_id,
                "2026-11-02T12:00:00+00:00",
                "2026-11-02T12:00:00+00:00",
            ),
        )
        self.connection.execute(
            """
            INSERT INTO races (
                record_id, name, started_at, distance_metres,
                result_duration_seconds, target_duration_seconds
            ) VALUES (?, 'Hamilton Marathon', '2026-11-08T12:00:00+00:00',
                      42195, NULL, 11400)
            """,
            (record_id,),
        )

    def _block(self, record_id: str, race_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id, record_type, timezone, created_at, updated_at,
                provenance_kind
            ) VALUES (?, 'training_block', 'America/Toronto', ?, ?,
                      'user_entered')
            """,
            (
                record_id,
                "2026-11-02T12:00:00+00:00",
                "2026-11-02T12:00:00+00:00",
            ),
        )
        self.connection.execute(
            """
            INSERT INTO training_blocks (
                record_id, goal, start_date, race_date
            ) VALUES (?, 'Hamilton build', '2026-11-02', '2026-11-08')
            """,
            (record_id,),
        )

    def _document(self, document_type: str, source_record_id: str) -> sqlite3.Row:
        row = self._optional_document(document_type, source_record_id)
        self.assertIsNotNone(row)
        return row

    def _optional_document(
        self,
        document_type: str,
        source_record_id: str,
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT *
            FROM retrieval_documents
            WHERE document_type = ? AND source_record_id = ?
            """,
            (document_type, source_record_id),
        ).fetchone()

    def _document_rows(self) -> list[tuple[object, ...]]:
        rows = self.connection.execute(
            """
            SELECT document_id, document_type, source_record_id,
                   source_updated_at, title, body, metadata_json,
                   evidence_json, caveats_json, document_version, stale_reason
            FROM retrieval_documents
            ORDER BY document_id
            """
        ).fetchall()
        return [tuple(row) for row in rows]


if __name__ == "__main__":
    unittest.main()
