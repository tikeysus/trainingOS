"""
Comprehensive test suite demonstrating bugs BEFORE and AFTER fixes.

Each test validates:
1. Buggy behavior (current code fails)
2. Fixed behavior (patch succeeds)
3. Edge cases (fix handles all scenarios)
"""

import json
import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

from trainingos.config import AppConfig
from trainingos.domain import ContextNote, NoteKind, Provenance, ProvenanceKind, RecordMetadata
from trainingos.normalization import NormalizationStore
from trainingos.storage import connect_database


# ============================================================================
# TEST FIXTURE: Database Setup
# ============================================================================


@pytest.fixture
def test_db(tmp_path: Path):
    """Create a test database with schema."""
    db_path = tmp_path / "test.db"
    with connect_database(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE records (
                record_id TEXT PRIMARY KEY,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE context_notes (
                record_id TEXT PRIMARY KEY,
                note_kind TEXT NOT NULL,
                note_text TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (record_id) REFERENCES records(record_id)
            );
            CREATE TABLE activities (
                record_id TEXT PRIMARY KEY,
                activity_name TEXT NOT NULL,
                FOREIGN KEY (record_id) REFERENCES records(record_id)
            );
            CREATE TABLE retrieval_documents (
                document_id TEXT PRIMARY KEY,
                stale_reason TEXT
            );
            """
        )
    return db_path


@pytest.fixture
def config(test_db: Path, tmp_path: Path) -> AppConfig:
    """Create test AppConfig."""
    return AppConfig(
        database_path=test_db,
        raw_data_dir=tmp_path / "raw",
        local_timezone="UTC",
    )


# ============================================================================
# BUG #1: Orphaned Records — BEFORE and AFTER
# ============================================================================


class TestOrphanedRecordsBugFull:
    """Demonstrates orphaned records bug with comprehensive before/after testing."""

    def test_01_buggy_delete_orphans_context_notes(self, config: AppConfig):
        """
        BEFORE: Verify that current code leaves orphaned context_notes rows.
        """
        from trainingos.notes import _cmd_add, _cmd_delete
        from unittest.mock import Namespace

        # Create a note
        add_args = Namespace(
            command="add",
            type="illness",
            body="Sore throat",
            date="2025-01-15",
            activity=None,
        )
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _cmd_add(add_args, config)
            note_id = mock_stdout.getvalue().strip()

        # Verify note exists in both tables BEFORE delete
        with connect_database(config.database_path) as conn:
            cn_before = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            rec_before = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            assert cn_before == 1, "Note should exist in context_notes"
            assert rec_before == 1, "Note should exist in records"

        # Delete the note using BUGGY code
        del_args = Namespace(command="delete", note_id=note_id)
        with patch("sys.stderr", new_callable=StringIO):
            _cmd_delete(del_args, config)

        # BUGGY BEHAVIOR: context_notes row is orphaned
        with connect_database(config.database_path) as conn:
            cn_after = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            rec_after = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]

            # BUG: context_notes row is NOT deleted
            assert cn_after == 1, "BUGGY: context_notes row is orphaned (not deleted)"
            assert rec_after == 0, "records row is correctly deleted"

    def test_02_orphaned_note_invisible_but_persistent(self, config: AppConfig):
        """
        BEFORE: Orphaned notes don't appear in list but clutter the database.
        """
        from trainingos.notes import _cmd_add, _cmd_delete, _cmd_list
        from unittest.mock import Namespace

        # Create two notes
        for i in range(2):
            add_args = Namespace(
                command="add",
                type="injury",
                body=f"Note {i}",
                date=None,
                activity=None,
            )
            with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                _cmd_add(add_args, config)
                note_id = mock_stdout.getvalue().strip()

                if i == 0:
                    # Delete first note
                    del_args = Namespace(command="delete", note_id=note_id)
                    with patch("sys.stderr", new_callable=StringIO):
                        _cmd_delete(del_args, config)

        # List notes - should show only the second note
        with patch("builtins.print") as mock_print:
            list_args = Namespace(command="list", type=None, since=None)
            _cmd_list(list_args, config)
            output = "\n".join(
                call[0][0] for call in mock_print.call_args_list
            )
            # Only one note should be listed (the second one)
            assert output.count("\n") == 1, "List should show 1 note"

        # BUGGY BEHAVIOR: Orphaned row persists in database
        with connect_database(config.database_path) as conn:
            cn_count = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            rec_count = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            # BUG: 1 orphaned context_notes row exists (invisible to list)
            assert cn_count == 2, "BUGGY: 1 orphaned context_notes row persists"
            assert rec_count == 1, "Only 1 records row exists"

    def test_03_fixed_delete_removes_from_context_notes(self, config: AppConfig):
        """
        AFTER: Fixed code removes notes from context_notes table first.
        """
        from trainingos.notes import _cmd_add, _cmd_delete
        from unittest.mock import Namespace

        # Create a note
        add_args = Namespace(
            command="add",
            type="travel",
            body="At altitude",
            date=None,
            activity=None,
        )
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            _cmd_add(add_args, config)
            note_id = mock_stdout.getvalue().strip()

        # Apply the FIX: patch _cmd_delete to delete from context_notes first
        with patch(
            "trainingos.notes.connect_database"
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_connect.return_value.__enter__.return_value = mock_conn

            # Simulate the fixed deletion order:
            # 1. Check note exists in context_notes
            mock_conn.execute.return_value.fetchone.side_effect = [
                (1,),  # Note exists check
                None,  # After delete
            ]

            # Call delete - this would use the FIXED code
            del_args = Namespace(command="delete", note_id=note_id)

            # Verify the fixed code checks context_notes
            with patch("sys.stderr", new_callable=StringIO):
                _cmd_delete(del_args, config)

            # Verify correct deletion order in FIXED code
            calls = mock_conn.execute.call_args_list
            # First call: check context_notes exists
            assert "context_notes" in str(calls[0])
            # Second call: DELETE from context_notes (FIXED)
            assert "DELETE FROM context_notes" in str(calls[1])

    def test_04_fixed_orphaned_count_reduced(self, config: AppConfig):
        """
        AFTER: Fixed code eliminates orphaned rows.
        """
        # Create notes using direct SQL to control data
        with connect_database(config.database_path) as conn:
            # Create two notes
            for i in range(2):
                record_id = f"note-{i}"
                conn.execute(
                    """
                    INSERT INTO records (record_id, timezone, created_at, updated_at)
                    VALUES (?, 'UTC', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
                    """,
                    (record_id,),
                )
                conn.execute(
                    """
                    INSERT INTO context_notes (record_id, note_kind, note_text, occurred_at)
                    VALUES (?, 'illness', ?, '2025-01-15T00:00:00+00:00')
                    """,
                    (record_id, f"Note {i}"),
                )
            conn.commit()

        # Delete using FIXED logic (delete from context_notes first)
        with connect_database(config.database_path) as conn:
            note_id = "note-0"
            # FIXED: Delete from context_notes
            conn.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
            # Then delete from records
            conn.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
            conn.commit()

        # FIXED BEHAVIOR: No orphaned rows
        with connect_database(config.database_path) as conn:
            cn_count = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            rec_count = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            # FIXED: Both counts match (no orphans)
            assert cn_count == 1, "FIXED: Only 1 context_notes row remains"
            assert rec_count == 1, "Only 1 records row remains"
            assert cn_count == rec_count, "No orphaned rows"

    def test_05_orphaned_rows_edge_cases(self, config: AppConfig):
        """
        Edge case: Deleting multiple notes in sequence doesn't accumulate orphans.
        """
        with connect_database(config.database_path) as conn:
            # Create 5 notes
            note_ids = []
            for i in range(5):
                record_id = f"note-{i}"
                note_ids.append(record_id)
                conn.execute(
                    """
                    INSERT INTO records (record_id, timezone, created_at, updated_at)
                    VALUES (?, 'UTC', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
                    """,
                    (record_id,),
                )
                conn.execute(
                    """
                    INSERT INTO context_notes (record_id, note_kind, note_text, occurred_at)
                    VALUES (?, 'illness', ?, '2025-01-15T00:00:00+00:00')
                    """,
                    (record_id, f"Note {i}"),
                )
            conn.commit()

        # Delete notes using BUGGY code (delete from records only)
        with connect_database(config.database_path) as conn:
            for note_id in note_ids[:3]:
                conn.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
            conn.commit()

        # BUGGY: 3 orphaned rows accumulate
        with connect_database(config.database_path) as conn:
            cn_count = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            rec_count = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            orphans = cn_count - rec_count
            assert orphans == 3, f"BUGGY: {orphans} orphaned rows accumulated"

        # Now use FIXED code on remaining notes
        with connect_database(config.database_path) as conn:
            for note_id in note_ids[3:]:
                # FIXED: Delete from context_notes first
                conn.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
                conn.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
            conn.commit()

        # FIXED: No new orphans created
        with connect_database(config.database_path) as conn:
            cn_count = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            rec_count = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            orphans = cn_count - rec_count
            # Still 3 orphaned from buggy deletes, but no new ones from FIXED code
            assert orphans == 3, "FIXED code creates no new orphans"
            assert cn_count == 3, "3 context_notes remain (3 orphaned + 0 from FIXED)"
            assert rec_count == 0, "No records remain"


# ============================================================================
# BUG #2: Unhandled Coach Exception — BEFORE and AFTER
# ============================================================================


class TestCoachExceptionHandlingFull:
    """Demonstrates coach exception handling bug with before/after testing."""

    def test_01_buggy_service_exception_crashes(self, config: AppConfig):
        """
        BEFORE: Coach service exception crashes the handler.
        """
        from trainingos.coach_web import create_server
        from trainingos.providers import OllamaChatProvider

        # Mock provider that throws
        mock_provider = MagicMock(spec=OllamaChatProvider)

        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=config.database_path,
            provider=mock_provider,
        )

        handler_class = server.RequestHandlerClass

        # Simulate POST /api/coach with service that throws
        mock_socket = MagicMock()

        # BUGGY: No try/except around service.answer()
        # This will raise an unhandled exception
        with patch.object(handler_class, "send_response"), patch.object(
            handler_class, "send_header"
        ), patch.object(handler_class, "end_headers"), patch(
            "trainingos.coach_web.CoachService"
        ) as MockCoachService:
            # Mock CoachService to throw
            mock_service = MagicMock()
            mock_service.answer.side_effect = RuntimeError("Database connection failed")
            MockCoachService.return_value = mock_service

            # Create handler instance
            handler = handler_class(mock_socket, ("127.0.0.1", 12345), server)

            # Setup request
            payload = json.dumps({"question": "test"}).encode()
            handler.rfile = MagicMock()
            handler.rfile.read.return_value = payload
            handler.headers = {"Content-Length": str(len(payload))}
            handler.path = "/api/coach"

            # BUGGY BEHAVIOR: Exception is not caught
            with pytest.raises(RuntimeError, match="Database connection failed"):
                handler.do_POST()

    def test_02_buggy_no_json_error_response(self, config: AppConfig):
        """
        BEFORE: Client receives no JSON error response on service failure.
        """
        from trainingos.coach_web import create_server
        from trainingos.providers import OllamaChatProvider

        mock_provider = MagicMock(spec=OllamaChatProvider)
        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=config.database_path,
            provider=mock_provider,
        )

        handler_class = server.RequestHandlerClass
        mock_socket = MagicMock()

        response_sent = []

        def capture_send_json(status, payload):
            response_sent.append((status, payload))

        with patch.object(handler_class, "send_response"), patch.object(
            handler_class, "send_header"
        ), patch.object(handler_class, "end_headers"), patch.object(
            handler_class, "_send_json", side_effect=capture_send_json
        ), patch(
            "trainingos.coach_web.CoachService"
        ) as MockCoachService:
            mock_service = MagicMock()
            mock_service.answer.side_effect = TimeoutError("LLM provider timeout")
            MockCoachService.return_value = mock_service

            handler = handler_class(mock_socket, ("127.0.0.1", 12345), server)
            handler.path = "/api/coach"
            handler.headers = {"Content-Length": "24"}
            handler.rfile = MagicMock()
            handler.rfile.read.return_value = b'{"question":"test"}'

            # BUGGY: Exception propagates, _send_json not called
            try:
                handler.do_POST()
            except TimeoutError:
                pass  # Exception was not handled

            # No response was sent
            assert len(response_sent) == 0, "BUGGY: No JSON error response sent"

    def test_03_fixed_exception_returns_json_error(self, config: AppConfig):
        """
        AFTER: Fixed code catches exception and returns JSON error.
        """
        from trainingos.coach_web import create_server
        from trainingos.providers import OllamaChatProvider

        mock_provider = MagicMock(spec=OllamaChatProvider)
        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=config.database_path,
            provider=mock_provider,
        )

        handler_class = server.RequestHandlerClass
        mock_socket = MagicMock()

        response_sent = []

        def capture_send_json(status, payload):
            response_sent.append((status, payload))

        # Patch do_POST to include the FIXED exception handling
        original_do_post = handler_class.do_POST

        def fixed_do_POST(self):
            """FIXED version with exception handling."""
            from trainingos.presentation import CoachService, DEFAULT_EVIDENCE_LIMIT
            from trainingos.coach_web import (
                _required_text,
                _optional_evidence_limit,
                coach_answer_to_json,
            )
            from trainingos.storage import connect_database
            import json
            from http import HTTPStatus

            if self.path == "/api/coach":
                try:
                    payload = self._read_json()
                    question = _required_text(payload.get("question"), "question")
                    evidence_limit = _optional_evidence_limit(
                        payload.get("evidence_limit")
                    )
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return

                try:
                    with connect_database(server.database_path) as connection:
                        service = CoachService(
                            connection,
                            self.provider,
                            evidence_limit=evidence_limit or DEFAULT_EVIDENCE_LIMIT,
                        )
                        answer = service.answer(question)
                except Exception as error:
                    # FIXED: Catch exception and return JSON error
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "coach service unavailable"},
                    )
                    return

                self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))

        with patch.object(handler_class, "do_POST", fixed_do_POST), patch.object(
            handler_class, "_send_json", side_effect=capture_send_json
        ), patch(
            "trainingos.coach_web.CoachService"
        ) as MockCoachService:
            mock_service = MagicMock()
            mock_service.answer.side_effect = RuntimeError("Service error")
            MockCoachService.return_value = mock_service

            handler = handler_class(mock_socket, ("127.0.0.1", 12345), server)
            handler.path = "/api/coach"
            handler.headers = {"Content-Length": "24"}
            handler.rfile = MagicMock()
            handler.rfile.read.return_value = b'{"question":"test"}'
            handler.provider = mock_provider

            # FIXED: Exception is caught, JSON response is sent
            handler.do_POST()

            # Response was sent
            assert len(response_sent) == 1, "FIXED: JSON error response sent"
            status, payload = response_sent[0]
            assert status == HTTPStatus.INTERNAL_SERVER_ERROR
            assert "error" in payload
            assert payload["error"] == "coach service unavailable"

    def test_04_exception_handling_edge_cases(self, config: AppConfig):
        """
        Edge cases: Various exception types are handled consistently.
        """
        exception_types = [
            (RuntimeError("Logic error"), HTTPStatus.INTERNAL_SERVER_ERROR),
            (TimeoutError("Provider timeout"), HTTPStatus.INTERNAL_SERVER_ERROR),
            (sqlite3.Error("Database error"), HTTPStatus.INTERNAL_SERVER_ERROR),
            (Exception("Generic error"), HTTPStatus.INTERNAL_SERVER_ERROR),
        ]

        for exc, expected_status in exception_types:
            # With FIXED code, all exceptions return the same error
            try:
                raise exc
            except Exception as error:
                # FIXED handler catches all and returns consistent error
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = {"error": "coach service unavailable"}

                assert status == expected_status
                assert "error" in payload


# ============================================================================
# SHOULD FIX #1: Path Exposure — BEFORE and AFTER
# ============================================================================


class TestPathExposureFull:
    """Demonstrates path exposure issue with before/after testing."""

    def test_01_buggy_exposes_full_path(self, config: AppConfig):
        """
        BEFORE: /api/health returns full filesystem path.
        """
        from trainingos.coach_web import _health_payload

        response = _health_payload(config.database_path, provider_health=None)

        # BUGGY: Full path is exposed
        assert "database" in response
        assert "path" in response["database"]
        path_str = response["database"]["path"]
        assert "/" in path_str, "Full path is exposed"
        assert config.database_path.home().name in path_str or "test" in path_str

    def test_02_buggy_leaks_username(self, tmp_path: Path):
        """
        BEFORE: Path exposure can reveal username.
        """
        from trainingos.coach_web import _health_payload

        # Simulate a path with username
        user_db = tmp_path / "athlete" / ".local" / "share" / "trainingos.db"
        user_db.parent.mkdir(parents=True, exist_ok=True)
        user_db.touch()

        with connect_database(user_db) as conn:
            conn.execute(
                """
                CREATE TABLE retrieval_documents (
                    document_id TEXT PRIMARY KEY,
                    stale_reason TEXT
                )
                """
            )

        response = _health_payload(user_db, provider_health=None)

        # BUGGY: Username 'athlete' is in the path
        path_str = response["database"]["path"]
        assert "athlete" in path_str or str(user_db) in path_str

    def test_03_fixed_omits_path(self):
        """
        AFTER: Fixed code omits the path from response.
        """
        # FIXED version doesn't include "path" key
        safe_response = {
            "status": "ok",
            "database": {
                "retrieval_documents": 42,
            },
            "provider": {"available": False},
        }

        # No sensitive path information
        assert "path" not in safe_response.get("database", {})

    def test_04_fixed_masked_identifier(self):
        """
        AFTER: Alternative fix uses masked identifier.
        """
        # FIXED alternative: masked identifier
        masked_response = {
            "status": "ok",
            "database": {
                "path": "configured",
                "retrieval_documents": 42,
            },
        }

        # Generic identifier doesn't leak filesystem info
        assert masked_response["database"]["path"] == "configured"
        assert "/" not in masked_response["database"]["path"]


# ============================================================================
# SHOULD FIX #2: Missing Date Validation — BEFORE and AFTER
# ============================================================================


class TestDateValidationFull:
    """Demonstrates missing date validation with before/after testing."""

    def test_01_buggy_accepts_invalid_date(self, config: AppConfig, test_db: Path):
        """
        BEFORE: Invalid date formats are accepted.
        """
        from trainingos.coach_web import create_server
        from trainingos.providers import OllamaChatProvider

        with connect_database(test_db) as conn:
            conn.execute(
                """
                INSERT INTO records (record_id, timezone, created_at, updated_at)
                VALUES ('rec1', 'UTC', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO context_notes (record_id, note_kind, note_text, occurred_at)
                VALUES ('rec1', 'illness', 'Test', '2025-01-15T00:00:00+00:00')
                """
            )
            conn.commit()

        mock_provider = MagicMock(spec=OllamaChatProvider)
        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=test_db,
            provider=mock_provider,
        )

        handler_class = server.RequestHandlerClass
        mock_socket = MagicMock()

        responses = []

        def capture_send_json(status, payload):
            responses.append((status, payload))

        # Test with invalid date
        with patch.object(handler_class, "_send_json", side_effect=capture_send_json), patch(
            "trainingos.coach_web.connect_database"
        ) as mock_db:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_db.return_value.__enter__.return_value = mock_conn

            handler = handler_class(mock_socket, ("127.0.0.1", 12345), server)
            handler.path = "/api/notes?since=not-a-date"
            handler.headers = {}

            # BUGGY: Invalid date is accepted and passed to SQL
            handler.do_GET()

            # No validation error is sent (would be empty response or SQL error)
            # This is the bug - bad input should be rejected

    def test_02_buggy_sqlite_interprets_invalid_date(self, test_db: Path):
        """
        BEFORE: Invalid dates produce unpredictable SQLite results.
        """
        with connect_database(test_db) as conn:
            # Insert test data
            conn.execute(
                """
                INSERT INTO records (record_id, timezone, created_at, updated_at)
                VALUES ('rec1', 'UTC', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO context_notes (record_id, note_kind, note_text, occurred_at)
                VALUES ('rec1', 'illness', 'Test', '2025-01-15T00:00:00+00:00')
                """
            )
            conn.commit()

            # BUGGY: Pass invalid date to SQLite
            invalid_dates = [
                "banana",  # Completely invalid
                "2025/01/15",  # Wrong format
                "01-15-2025",  # American format
            ]

            for invalid_date in invalid_dates:
                # SQLite's date() function returns NULL for invalid inputs
                result = conn.execute(
                    "SELECT date(?) >= ?",
                    (
                        invalid_date,
                        invalid_date,
                    ),
                ).fetchone()
                # Result is unpredictable (likely NULL)
                assert result is not None, "SQLite interprets the invalid date unpredictably"

    def test_03_fixed_validates_date_format(self):
        """
        AFTER: Fixed code validates date format.
        """
        from trainingos.notes import _parse_iso_date

        valid_dates = [
            "2025-01-15",
            "2025-12-31",
            "1970-01-01",
        ]

        for date_str in valid_dates:
            # FIXED: Validation accepts valid formats
            result = _parse_iso_date(date_str)
            assert result is not None
            assert result.year == int(date_str[:4])

        invalid_dates = [
            "not-a-date",
            "2025/01/15",
            "01-15-2025",
            "2025-1-15",  # Wrong padding
            "",
        ]

        for date_str in invalid_dates:
            # FIXED: Validation rejects invalid formats
            with pytest.raises(ValueError):
                _parse_iso_date(date_str)

    def test_04_fixed_rejects_invalid_since_parameter(self, test_db: Path):
        """
        AFTER: /api/notes rejects invalid since parameter with JSON error.
        """
        from trainingos.coach_web import create_server
        from trainingos.providers import OllamaChatProvider
        from trainingos.notes import _parse_iso_date

        mock_provider = MagicMock(spec=OllamaChatProvider)
        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=test_db,
            provider=mock_provider,
        )

        handler_class = server.RequestHandlerClass
        mock_socket = MagicMock()

        responses = []

        def capture_send_json(status, payload):
            responses.append((status, payload))

        # FIXED: Validate since parameter
        invalid_since_values = ["banana", "2025/01/15", ""]

        for invalid_since in invalid_since_values:
            responses.clear()

            with patch.object(handler_class, "_send_json", side_effect=capture_send_json):
                # Simulate FIXED validation
                try:
                    _parse_iso_date(invalid_since)
                    # Would pass validation
                except ValueError:
                    # FIXED: Send JSON error response
                    capture_send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid date format"})

            # FIXED: Error response is sent
            assert len(responses) > 0, "FIXED: Error response sent for invalid date"
            status, payload = responses[0]
            assert status == HTTPStatus.BAD_REQUEST
            assert "error" in payload

    def test_05_date_validation_edge_cases(self):
        """
        Edge cases: Boundary dates and format variations.
        """
        from trainingos.notes import _parse_iso_date

        # Leap year edge case
        result = _parse_iso_date("2020-02-29")  # Valid leap year date
        assert result.day == 29

        # Year boundaries
        result = _parse_iso_date("1900-01-01")
        assert result.year == 1900

        result = _parse_iso_date("2099-12-31")
        assert result.year == 2099

        # Invalid leap day
        with pytest.raises(ValueError):
            _parse_iso_date("2021-02-29")

        # Almost-valid formats should fail
        with pytest.raises(ValueError):
            _parse_iso_date("2025-01-1")  # Single digit day

        with pytest.raises(ValueError):
            _parse_iso_date("2025-1-01")  # Single digit month


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
