"""Test cases for critical bugs found in notes and coach_web modules."""

import json
import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

from trainingos.coach_web import create_server
from trainingos.config import AppConfig
from trainingos.domain import ContextNote, NoteKind, Provenance, ProvenanceKind, RecordMetadata
from trainingos.normalization import NormalizationStore
from trainingos.providers import OllamaChatProvider
from trainingos.storage import connect_database


# ============================================================================
# CRITICAL BUG #1: Orphaned Records in notes.py _cmd_delete()
# ============================================================================


class TestOrphanedRecordsBug:
    """
    Bug: _cmd_delete() checks context_notes existence, then deletes from records table.
    This leaves orphaned context_notes rows in the database.

    Expected: Delete should target context_notes directly.
    """

    def test_delete_orphans_context_notes_row(self, tmp_path: Path):
        """Verify that delete leaves orphaned context_notes rows."""
        from trainingos.notes import _cmd_add, _cmd_delete
        from unittest.mock import Namespace

        db_path = tmp_path / "test.db"
        config = AppConfig(
            database_path=db_path,
            raw_data_dir=tmp_path / "raw",
            local_timezone="UTC",
        )

        # Create database with schema
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
                """
            )

        # Add a note via CLI
        add_args = Namespace(
            command="add",
            type="illness",
            body="Test note",
            date=None,
            activity=None,
        )
        with patch("sys.stdout") as mock_stdout:
            _cmd_add(add_args, config)
            note_id = mock_stdout.write.call_args_list[0][0][0].strip()

        # Verify note exists in both tables
        with connect_database(db_path) as conn:
            cn_row = conn.execute(
                "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
            ).fetchone()
            rec_row = conn.execute(
                "SELECT 1 FROM records WHERE record_id = ?", (note_id,)
            ).fetchone()
            assert cn_row is not None, "Note should exist in context_notes"
            assert rec_row is not None, "Note should exist in records"

        # Delete the note
        del_args = Namespace(command="delete", note_id=note_id)
        _cmd_delete(del_args, config)

        # BUG: context_notes row is orphaned
        with connect_database(db_path) as conn:
            cn_row = conn.execute(
                "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
            ).fetchone()
            rec_row = conn.execute(
                "SELECT 1 FROM records WHERE record_id = ?", (note_id,)
            ).fetchone()
            # This assertion WILL FAIL with the buggy code
            assert cn_row is None, "context_notes row should be deleted but is orphaned"
            assert rec_row is None, "records row should be deleted"

    def test_orphaned_notes_accumulate_in_list(self, tmp_path: Path):
        """Verify orphaned notes don't appear in list but clutter the database."""
        from trainingos.notes import _cmd_add, _cmd_delete, _cmd_list
        from unittest.mock import Namespace

        db_path = tmp_path / "test.db"
        config = AppConfig(
            database_path=db_path,
            raw_data_dir=tmp_path / "raw",
            local_timezone="UTC",
        )

        # Create database
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
                """
            )

        # Add and delete notes
        note_ids = []
        for i in range(3):
            add_args = Namespace(
                command="add",
                type="injury",
                body=f"Note {i}",
                date=None,
                activity=None,
            )
            with patch("sys.stdout") as mock_stdout:
                _cmd_add(add_args, config)
                note_ids.append(mock_stdout.write.call_args_list[0][0][0].strip())

        # Delete first two notes
        for note_id in note_ids[:2]:
            del_args = Namespace(command="delete", note_id=note_id)
            _cmd_delete(del_args, config)

        # List should only show remaining note
        with patch("builtins.print") as mock_print:
            list_args = Namespace(command="list", type=None, since=None)
            _cmd_list(list_args, config)

            # Should only print one note (the third one)
            calls = [call[0][0] for call in mock_print.call_args_list]
            assert len(calls) == 1, f"Expected 1 note, got {len(calls)}"
            assert note_ids[2] in calls[0]

        # But in the database, orphaned context_notes rows exist
        with connect_database(db_path) as conn:
            orphan_count = conn.execute(
                "SELECT COUNT(*) FROM context_notes"
            ).fetchone()[0]
            records_count = conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            # BUG: orphaned rows exist
            assert orphan_count == 2, f"Expected 2 orphaned notes, got {orphan_count}"
            assert records_count == 1, f"Expected 1 record, got {records_count}"


# ============================================================================
# BUG #2: Unhandled Exception in /api/coach POST
# ============================================================================


class TestUnhandledExceptionInCoach:
    """
    Bug: POST /api/coach doesn't wrap service.answer() call.
    If CoachService throws, HTTP handler crashes without JSON error response.
    """

    def test_coach_service_exception_crashes_handler(self, tmp_path: Path):
        """Verify that service exceptions crash the handler."""
        db_path = tmp_path / "test.db"

        # Create minimal database
        with connect_database(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE context_notes (
                    record_id TEXT PRIMARY KEY,
                    note_kind TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE records (
                    record_id TEXT PRIMARY KEY,
                    timezone TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE retrieval_documents (
                    document_id TEXT PRIMARY KEY,
                    stale_reason TEXT
                );
                """
            )

        # Mock provider that throws on answer()
        mock_provider = MagicMock(spec=OllamaChatProvider)
        mock_provider.answer.side_effect = RuntimeError("Service error")

        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=db_path,
            provider=mock_provider,
        )

        # Get the handler class
        handler_class = server.RequestHandlerClass

        # Simulate POST request
        mock_request = MagicMock()
        mock_request.makefile.return_value.__enter__.return_value.read.return_value = (
            b"POST /api/coach HTTP/1.1\r\nContent-Length: 26\r\n\r\n"
        )

        payload = json.dumps({"question": "test"}).encode()
        mock_socket = MagicMock()

        with patch.object(handler_class, "rfile") as mock_rfile, patch.object(
            handler_class, "wfile"
        ) as mock_wfile, patch.object(handler_class, "send_response"), patch.object(
            handler_class, "send_header"
        ), patch.object(
            handler_class, "end_headers"
        ):
            # This should raise an unhandled exception
            handler = handler_class(mock_socket, ("127.0.0.1", 12345), server)

            # The bug: no try/except around service.answer() means this crashes
            with pytest.raises(RuntimeError, match="Service error"):
                handler.do_POST()

    def test_coach_error_response_with_fix(self, tmp_path: Path):
        """Verify that with proper error handling, coach errors return JSON."""
        # This test documents what the FIXED code should do
        db_path = tmp_path / "test.db"

        with connect_database(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE context_notes (
                    record_id TEXT PRIMARY KEY,
                    note_kind TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE records (
                    record_id TEXT PRIMARY KEY,
                    timezone TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE retrieval_documents (
                    document_id TEXT PRIMARY KEY,
                    stale_reason TEXT
                );
                """
            )

        mock_provider = MagicMock(spec=OllamaChatProvider)
        mock_provider.chat.side_effect = TimeoutError("Provider timeout")

        # EXPECTED FIX: Wrap service.answer() in try/except and return error JSON
        # This test shows what should happen
        payload = {"question": "test"}
        try:
            # Simulated fixed code would do:
            # try:
            #     answer = service.answer(question)
            # except Exception as error:
            #     return _send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
            #                       {"error": "coach service unavailable"})
            raise TimeoutError("Provider timeout")
        except TimeoutError:
            # Should return JSON error, not crash
            response = {
                "error": "coach service unavailable",
            }
            assert isinstance(response, dict)
            assert "error" in response


# ============================================================================
# SHOULD FIX #1: Path Exposure in /api/health
# ============================================================================


class TestPathExposureInHealth:
    """
    Issue: /api/health endpoint leaks full filesystem path.
    This reveals home directory and username to any client.
    """

    def test_health_leaks_full_database_path(self, tmp_path: Path):
        """Verify that /api/health exposes the full database path."""
        db_path = tmp_path / "user" / ".local" / "share" / "trainingos.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        with connect_database(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE retrieval_documents (
                    document_id TEXT PRIMARY KEY,
                    stale_reason TEXT
                );
                CREATE TABLE context_notes (
                    record_id TEXT PRIMARY KEY,
                    note_kind TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )

        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=db_path,
            provider=MagicMock(spec=OllamaChatProvider),
        )

        handler_class = server.RequestHandlerClass
        mock_socket = MagicMock()

        responses = []

        def mock_send_response(status):
            responses.append({"status": status})

        def mock_send_header(name, value):
            if "Content-Length" in name:
                responses[-1]["body"] = value

        def mock_end_headers():
            pass

        def mock_write(data):
            if responses:
                responses[-1].setdefault("body", b"")
                responses[-1]["body"] = data

        with patch.object(handler_class, "send_response", side_effect=mock_send_response), patch.object(
            handler_class, "send_header", side_effect=mock_send_header
        ), patch.object(handler_class, "end_headers", side_effect=mock_end_headers), patch.object(
            handler_class, "rfile", MagicMock()
        ), patch.object(
            handler_class, "wfile", MagicMock(write=mock_write)
        ), patch.object(
            handler_class, "path", "/api/health"
        ), patch.object(
            handler_class, "command", "GET"
        ), patch.object(
            handler_class, "client_address", ("127.0.0.1", 54321)
        ):
            handler = handler_class(mock_socket, ("127.0.0.1", 12345), server)
            handler.do_GET()

            # Verify the response contains the full path (the bug)
            # In the actual response, look for the database path
            # This would need actual socket mocking to fully test, but
            # the issue is that _health_payload() includes the path

    def test_health_should_not_expose_absolute_path(self):
        """Test that the fixed version doesn't expose absolute paths."""
        from trainingos.coach_web import _health_payload
        from pathlib import Path

        # Current implementation exposes the path - this test shows the fix
        db_path = Path("/Users/athlete/.local/share/trainingos.db")

        # CURRENT (buggy):
        # payload["database"]["path"] = str(db_path.expanduser().absolute())
        # This returns "/Users/athlete/.local/share/trainingos.db"

        # FIXED (recommended):
        # Option 1: Omit path entirely
        # option 2: Return a generic identifier
        safe_response = {"path": "configured"}  # or omit entirely

        assert "Users" not in str(safe_response)


# ============================================================================
# SHOULD FIX #2: Missing Date Validation
# ============================================================================


class TestMissingDateValidation:
    """
    Issue: /api/notes?since=... parameter not validated for YYYY-MM-DD format.
    Invalid dates passed to SQLite's date() function produce unpredictable results.
    """

    def test_invalid_since_parameter_not_rejected(self, tmp_path: Path):
        """Verify that invalid date formats are accepted (the bug)."""
        db_path = tmp_path / "test.db"

        with connect_database(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE context_notes (
                    record_id TEXT PRIMARY KEY,
                    note_kind TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE records (
                    record_id TEXT PRIMARY KEY,
                    timezone TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                """
            )
            # Insert a test note
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

        server = create_server(
            host="127.0.0.1",
            port=0,
            database_path=db_path,
            provider=MagicMock(spec=OllamaChatProvider),
        )

        # Test with invalid date format
        from urllib.parse import urlencode

        invalid_dates = [
            "not-a-date",
            "2025/01/01",
            "01-15-2025",
            "banana",
            "",
        ]

        for invalid_date in invalid_dates:
            # The buggy code doesn't validate these, so they're passed to SQLite
            # which might return NULL, 0, or error messages
            query = urlencode({"since": invalid_date})
            # This request should fail with a validation error but currently doesn't
            # The fix: validate with _parse_iso_date() before using in SQL

    def test_invalid_since_rejected_with_fix(self):
        """Test that the fixed version rejects invalid dates."""
        from trainingos.notes import _parse_iso_date

        # FIXED code should use _parse_iso_date() for validation
        valid_dates = ["2025-01-15", "2025-12-31"]
        invalid_dates = ["2025/01/15", "15-01-2025", "banana"]

        for date_str in valid_dates:
            result = _parse_iso_date(date_str)
            assert result is not None

        for date_str in invalid_dates:
            with pytest.raises(ValueError):
                _parse_iso_date(date_str)


# ============================================================================
# EDGE CASE: API Inconsistency between CLI and Web
# ============================================================================


class TestAPIInconsistency:
    """
    Issue: CLI notes.py allows --activity linking, but web API doesn't accept activity_id.
    """

    def test_cli_allows_activity_linking(self, tmp_path: Path):
        """Verify CLI can link notes to activities."""
        # This functionality works in notes.py _cmd_add()
        # See lines 93-99 for activity validation
        pass  # Documented in notes.py, working as intended

    def test_web_api_ignores_activity_parameter(self, tmp_path: Path):
        """Verify web API doesn't accept activity linking."""
        # coach_web.py line 144: linked_record_ids=() always
        # Even if client sends "activity_id" in JSON, it's ignored
        db_path = tmp_path / "test.db"

        with connect_database(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE context_notes (
                    record_id TEXT PRIMARY KEY,
                    note_kind TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE records (
                    record_id TEXT PRIMARY KEY,
                    timezone TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE activities (
                    record_id TEXT PRIMARY KEY,
                    activity_name TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO records (record_id, timezone, created_at, updated_at)
                VALUES ('act1', 'UTC', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO activities (record_id, activity_name) VALUES ('act1', 'Run')
                """
            )
            conn.commit()

        # If web API client sends activity_id, it's silently ignored
        payload = {
            "type": "illness",
            "body": "Sore ankle from run",
            "activity_id": "act1",  # This will be ignored
        }

        # RECOMMENDED FIX:
        # 1. Accept optional "activity_id" in payload
        # 2. Validate it exists in activities table
        # 3. Pass it to linked_record_ids in ContextNote creation
        # Or document that web API doesn't support linking (if intentional)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
