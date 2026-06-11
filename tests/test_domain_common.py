from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

from trainingos.domain import (
    Measurement,
    MethodVersion,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    SourceReference,
    Unit,
)


class DomainCommonTests(unittest.TestCase):
    def test_source_reference_normalizes_sync_time_to_utc(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        reference = SourceReference(
            source="fixture",
            external_id="activity-42",
            synced_at=datetime(2026, 6, 11, 8, 30, tzinfo=eastern),
            raw_reference="sha256:abc",
        )

        self.assertEqual(datetime(2026, 6, 11, 12, 30, tzinfo=UTC), reference.synced_at)

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SourceReference("fixture", "42", datetime(2026, 6, 11, 12, 30))

    def test_metadata_requires_iana_timezone(self) -> None:
        now = datetime(2026, 6, 11, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "valid IANA timezone"):
            RecordMetadata("record-1", "EDT", now, now)

    def test_metadata_supports_local_records_without_external_id(self) -> None:
        now = datetime(2026, 6, 11, tzinfo=UTC)
        metadata = RecordMetadata(
            record_id="note-1",
            timezone="America/Toronto",
            created_at=now,
            updated_at=now,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        )

        self.assertEqual((), metadata.source_references)

    def test_computed_provenance_requires_method_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "versioned method"):
            Provenance(ProvenanceKind.COMPUTED)

        provenance = Provenance(
            ProvenanceKind.COMPUTED,
            method=MethodVersion("aerobic_efficiency", "1.0.0"),
            evidence_record_ids=("activity-1",),
        )
        self.assertEqual("1.0.0", provenance.method.version)

    def test_measurements_require_finite_values_and_explicit_units(self) -> None:
        measurement = Measurement(42195.0, Unit.METRE)
        self.assertEqual(Unit.METRE, measurement.unit)
        with self.assertRaisesRegex(ValueError, "finite"):
            Measurement(float("nan"), Unit.METRE)


if __name__ == "__main__":
    unittest.main()
