# Critical Bugs and Security Issues — Action Items

**Review Date:** 2026-07-04  
**Branch:** feat/issue-48-strava-adapter  
**Test Suite:** `tests/test_critical_bugs.py`

## Summary

Two agents performed parallel security and code-quality audits. **1 CRITICAL bug**, **1 BUG**, and **2 SHOULD FIX** issues identified. No SQL injection or command-injection vulnerabilities found.

---

## CRITICAL BUG

### Issue #1: Orphaned Records in `notes.py` — _cmd_delete()

**Location:** `src/trainingos/notes.py:164–171`

**Problem:**  
The delete function checks that a note exists in `context_notes` (line 164–166), then deletes from the `records` table (line 170). This leaves the `context_notes` row orphaned in the database because:

1. The note is verified to exist in `context_notes`
2. Deletion targets `records` table instead
3. Cascade delete doesn't clean up `context_notes` (unless explicitly configured)

**Impact:**  
- Database bloats with unreferenced `context_notes` rows
- `_cmd_list()` won't show orphaned rows (they're missing from `records`), so they're invisible but persistent
- Over time, accumulates garbage data

**Current Code:**
```python
def _cmd_delete(args: argparse.Namespace, config: AppConfig) -> None:
    note_id = args.note_id
    with connect_database(config.database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            sys.stderr.write(f"note not found: {note_id!r}\n")
            sys.exit(1)
        connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
        connection.commit()
```

**Fix:**  
Delete from `context_notes` directly. The cascade to `records` will handle cleanup if configured, or explicitly delete from both tables.

```python
def _cmd_delete(args: argparse.Namespace, config: AppConfig) -> None:
    note_id = args.note_id
    with connect_database(config.database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            sys.stderr.write(f"note not found: {note_id!r}\n")
            sys.exit(1)
        # Delete the note (cascade to records table if FK configured)
        connection.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
        # Explicitly clean records if cascade is not configured
        connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
        connection.commit()
```

**Test:** `tests/test_critical_bugs.py::TestOrphanedRecordsBug::test_delete_orphans_context_notes_row`

---

## BUG

### Issue #2: Unhandled Exception in `/api/coach` POST

**Location:** `src/trainingos/coach_web.py:161–167`

**Problem:**  
The `/api/coach` POST handler calls `service.answer(question)` without a try/except block. If `CoachService` throws (database error, provider timeout, malformed evidence), the HTTP handler crashes without returning a JSON error response.

**Impact:**  
- Client receives no response (or a raw exception traceback)
- Server logs are polluted with unhandled exceptions
- Degrades coach availability; no graceful failure mode

**Current Code:**
```python
def do_POST(self) -> None:
    # ... earlier POST routes ...
    if self.path != "/api/coach":
        # ...
        return
    try:
        payload = self._read_json()
        question = _required_text(payload.get("question"), "question")
        evidence_limit = _optional_evidence_limit(payload.get("evidence_limit"))
    except ValueError as error:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return
    with connect_database(database_path) as connection:
        service = CoachService(
            connection,
            provider,
            evidence_limit=evidence_limit or DEFAULT_EVIDENCE_LIMIT,
        )
        answer = service.answer(question)  # ← NO TRY/EXCEPT
    self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))
```

**Fix:**  
Wrap the service call and database access in try/except.

```python
try:
    with connect_database(database_path) as connection:
        service = CoachService(
            connection,
            provider,
            evidence_limit=evidence_limit or DEFAULT_EVIDENCE_LIMIT,
        )
        answer = service.answer(question)
except (sqlite3.Error, TimeoutError, Exception) as error:
    self._send_json(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        {"error": "coach service unavailable"},
    )
    return
self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))
```

**Test:** `tests/test_critical_bugs.py::TestUnhandledExceptionInCoach`

---

## SHOULD FIX #1: Path Exposure in `/api/health`

**Location:** `src/trainingos/coach_web.py:269` in `_health_payload()`

**Problem:**  
The `/api/health` endpoint returns the full expanded filesystem path:

```python
"path": str(database_path.expanduser().absolute()),
```

Example response:
```json
{
  "status": "ok",
  "database": {
    "path": "/Users/athlete/.local/share/trainingos/trainingos.sqlite3"
  }
}
```

This leaks:
- Home directory (`/Users/athlete`)
- System username (`athlete`)
- Exact file location

While the coach only listens on `127.0.0.1:8765`, any tool or person with local network access can query `/api/health` and discover filesystem paths.

**Fix:** Either omit the path or return a masked identifier:

```python
# Option 1: Omit entirely
payload["database"] = {"retrieval_documents": ...}

# Option 2: Mask with identifier
payload["database"] = {
    "path": "configured",
    "retrieval_documents": ...
}
```

**Test:** `tests/test_critical_bugs.py::TestPathExposureInHealth`

---

## SHOULD FIX #2: Missing Date Validation in `/api/notes` GET

**Location:** `src/trainingos/coach_web.py:62–64`

**Problem:**  
The `since` query parameter is not validated before being passed to SQLite's `date()` function:

```python
if since_param is not None:
    filters.append("date(note.occurred_at) >= ?")
    params.append(since_param)  # ← No format validation
```

If a client sends `?since=banana`, SQLite's `date()` function will:
- Return NULL (silently)
- Or return unpredictable results
- Or error with an unclear message

Compare to `notes.py` CLI, which validates strictly:

```python
if args.since is not None:
    try:
        since_dt = _parse_iso_date(args.since)  # Validates YYYY-MM-DD
    except ValueError as error:
        sys.stderr.write(f"error: {error}\n")
        sys.exit(1)
```

**Fix:**  
Validate `since_param` before use:

```python
if since_param is not None:
    try:
        since_dt = _parse_iso_date(since_param)  # From trainingos.notes
    except ValueError as error:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return
    filters.append("date(note.occurred_at) >= ?")
    params.append(since_dt.date().isoformat())
```

**Test:** `tests/test_critical_bugs.py::TestMissingDateValidation`

---

## EDGE CASE: API Inconsistency

**Location:** `src/trainingos/notes.py` vs `src/trainingos/coach_web.py`

**Issue:**  
The CLI `trainingos.notes add --activity <id>` supports linking notes to activities (notes.py:93–99), but the web API POST `/api/notes` ignores any `activity_id` parameter and always sets `linked_record_ids=()` (coach_web.py:144).

**Fix (if feature is intended):**  
Accept optional `activity_id` in the JSON payload and validate it:

```python
activity_id = payload.get("activity_id")
linked_record_ids = ()
if activity_id is not None:
    if not isinstance(activity_id, str):
        raise ValueError("activity_id must be a string")
    # Validate activity exists
    activity_row = connection.execute(
        "SELECT 1 FROM activities WHERE record_id = ?", (activity_id,)
    ).fetchone()
    if activity_row is None:
        raise ValueError(f"activity not found: {activity_id!r}")
    linked_record_ids = (activity_id,)
```

If linking is not intended for the web API, document it.

**Test:** `tests/test_critical_bugs.py::TestAPIInconsistency`

---

## Test Execution

Run all critical-bug tests:

```bash
pytest tests/test_critical_bugs.py -v
```

Each test class documents:
1. **What the bug is** (description in docstring)
2. **Why it's wrong** (test assertions)
3. **What the fix should do** (comments showing fixed behavior)

---

## Recommended Fix Order

1. **CRITICAL** → Fix orphaned records deletion (notes.py)
2. **BUG** → Wrap coach service exception handling (coach_web.py)
3. **SHOULD FIX** → Validate date format in GET /api/notes (coach_web.py)
4. **SHOULD FIX** → Mask or omit path in /api/health (coach_web.py)
5. **EDGE CASE** → Document or implement activity linking in web API (coach_web.py)
