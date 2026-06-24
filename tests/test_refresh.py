from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from trainingos.config import DATABASE_PATH_ENV, AppConfig
from trainingos.domain import (
    Activity,
    ActivityType,
    Measurement,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
    Unit,
)
from trainingos.normalization import NormalizationStore
from trainingos.refresh import main, refresh_training_data
from trainingos.storage import apply_migrations, connect_database


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "training.sqlite3"
        self.connection = connect_database(self.database_path)
        apply_migrations(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_refresh_generates_dashboard_and_coach_data(self) -> None:
        self._activity("run-1", datetime(2026, 6, 16, 11, 0, tzinfo=UTC), 3600, 10000)

        self.assertEqual(0, self._count("dashboard_weekly_training"))
        self.assertEqual(0, self._count("dashboard_metric_timeseries"))
        self.assertEqual(0, self._count("dashboard_evidence_documents"))

        report = refresh_training_data(
            self.connection,
            timezone="America/Toronto",
            now=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        )

        self.assertEqual(1, report.metrics.week_count)
        self.assertGreater(report.metrics.metric_count, 0)
        self.assertGreater(report.retrieval.generated_count, 0)
        self.assertGreater(self._count("dashboard_weekly_training"), 0)
        self.assertGreater(self._count("dashboard_metric_timeseries"), 0)
        self.assertGreater(self._count("dashboard_evidence_documents"), 0)

    def test_main_wires_config_migrations_refresh_and_summary(self) -> None:
        connection = unittest.mock.Mock()
        report = unittest.mock.Mock()
        report.metrics.week_count = 2
        report.metrics.metric_count = 12
        report.retrieval.generated_count = 5
        report.retrieval.stale_count = 0
        report.retrieval.deleted_count = 1

        with (
            patch.object(
                AppConfig,
                "from_env",
                return_value=AppConfig(
                    database_path=self.database_path,
                    raw_data_dir=self.root / "raw",
                    local_timezone="America/Toronto",
                ),
            ) as from_env,
            patch("trainingos.refresh.connect_database", return_value=connection) as connect_mock,
            patch("trainingos.refresh.apply_migrations") as migrations_mock,
            patch("trainingos.refresh.refresh_training_data", return_value=report) as refresh_mock,
            patch("sys.stdout") as stdout,
        ):
            exit_code = main(["--timezone", "UTC"])

        self.assertEqual(0, exit_code)
        from_env.assert_called_once_with()
        connect_mock.assert_called_once_with(self.database_path)
        migrations_mock.assert_called_once_with(connection)
        refresh_mock.assert_called_once_with(connection, timezone="UTC")
        connection.close.assert_called_once_with()
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("2 weeks", output)
        self.assertIn("12 metrics", output)
        self.assertIn("5 retrieval documents", output)

    def test_refresh_rolls_back_when_generation_fails(self) -> None:
        connection = unittest.mock.Mock()

        with patch(
            "trainingos.refresh.derive_training_metrics",
            side_effect=ValueError("bad timezone"),
        ):
            with self.assertRaisesRegex(ValueError, "bad timezone"):
                refresh_training_data(connection, timezone="Bad/Zone")

        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()

    def test_cli_rejects_placeholder_database_without_traceback(self) -> None:
        env = _pythonpath_env()
        env[DATABASE_PATH_ENV] = "/absolute/path/to/trainingos.sqlite3"

        result = subprocess.run(
            [sys.executable, "-m", "trainingos.refresh"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("documentation placeholder", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def _activity(
        self,
        record_id: str,
        started_at: datetime,
        duration_seconds: float,
        distance_metres: float,
    ) -> None:
        store = NormalizationStore(self.connection)
        store.upsert_activity(
            Activity(
                metadata=RecordMetadata(
                    record_id=record_id,
                    timezone="America/Toronto",
                    provenance=Provenance(ProvenanceKind.USER_ENTERED),
                    created_at=started_at,
                    updated_at=started_at,
                ),
                activity_type=ActivityType.RUN,
                started_at=started_at,
                duration=Measurement(duration_seconds, Unit.SECOND),
                distance=Measurement(distance_metres, Unit.METRE),
                title="Refresh fixture run",
            )
        )
        self.connection.commit()

    def _count(self, table: str) -> int:
        allowed_tables = {
            "dashboard_weekly_training",
            "dashboard_metric_timeseries",
            "dashboard_evidence_documents",
        }
        if table not in allowed_tables:
            raise ValueError("unsupported table")
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else src_path
    )
    return env


if __name__ == "__main__":
    unittest.main()
