from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta, timezone

from trainingos.domain import (
    Activity,
    ActivityType,
    Measurement,
    MethodVersion,
    MetricEvidence,
    MetricValue,
    Provenance,
    ProvenanceKind,
    Race,
    RecordMetadata,
    TrainingBlock,
    Unit,
)


def metadata(
    record_id: str = "record-1",
    provenance: Provenance | None = None,
) -> RecordMetadata:
    now = datetime(2026, 6, 11, tzinfo=UTC)
    return RecordMetadata(
        record_id=record_id,
        timezone="America/Toronto",
        created_at=now,
        updated_at=now,
        provenance=provenance,
    )


class DomainModelTests(unittest.TestCase):
    def test_activity_normalizes_start_time_and_uses_explicit_units(self) -> None:
        activity = Activity(
            metadata=metadata(),
            activity_type=ActivityType.RUN,
            started_at=datetime(
                2026, 6, 11, 6, 0, tzinfo=timezone(timedelta(hours=-4))
            ),
            duration=Measurement(3600, Unit.SECOND),
            distance=Measurement(15000, Unit.METRE),
        )

        self.assertEqual(datetime(2026, 6, 11, 10, 0, tzinfo=UTC), activity.started_at)
        self.assertEqual(Unit.METRE, activity.distance.unit)

    def test_activity_rejects_dimensionally_wrong_units(self) -> None:
        with self.assertRaisesRegex(ValueError, "duration must use s"):
            Activity(
                metadata=metadata(),
                activity_type=ActivityType.RUN,
                started_at=datetime(2026, 6, 11, tzinfo=UTC),
                duration=Measurement(3600, Unit.METRE),
            )

    def test_training_block_rejects_reversed_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not precede"):
            TrainingBlock(
                metadata=metadata(),
                name="Hamilton",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 8, 1),
            )

    def test_race_requires_positive_distance(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            Race(
                metadata=metadata(),
                name="Fixture Marathon",
                started_at=datetime(2026, 10, 1, tzinfo=UTC),
                distance=Measurement(0, Unit.METRE),
            )

    def test_metric_evidence_requires_versioned_computed_provenance(self) -> None:
        provenance = Provenance(
            ProvenanceKind.COMPUTED,
            method=MethodVersion("heart_rate_drift", "1.0.0"),
            evidence_record_ids=("activity-1", "sample-set-1"),
            caveats=("Requires steady-state effort",),
        )
        evidence = MetricEvidence(
            metadata=metadata("metric-1", provenance),
            metric=MetricValue("heart_rate_drift", Measurement(4.2, Unit.PERCENT)),
            window_start=datetime(2026, 6, 11, 10, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 11, 11, 0, tzinfo=UTC),
        )

        self.assertEqual("heart_rate_drift", evidence.metadata.provenance.method.name)

        with self.assertRaisesRegex(ValueError, "computed provenance"):
            MetricEvidence(
                metadata=metadata(
                    "metric-2", Provenance(ProvenanceKind.MODEL_INTERPRETED)
                ),
                metric=evidence.metric,
                window_start=evidence.window_start,
                window_end=evidence.window_end,
            )


if __name__ == "__main__":
    unittest.main()
