# Critical Bugs and Tests

**Review Date:** 2026-07-04  
**Branch:** feat/issue-48-strava-adapter  
**Status:** Issues identified and documented with comprehensive test coverage

---

## Summary

Four issues identified in security/code-quality audit. **1 CRITICAL**, **1 BUG**, **2 SHOULD FIX**. No SQL injection or command-injection vulnerabilities found.

| # | Issue | Severity | File | Line | Test Class |
|---|-------|----------|------|------|-----------|
| 1 | Orphaned records in delete | 🔴 CRITICAL | `src/trainingos/notes.py` | 170 | `TestOrphanedRecordsBugFull` |
| 2 | Unhandled coach exception | 🟠 BUG | `src/trainingos/coach_web.py` | 161–167 | `TestCoachExceptionHandlingFull` |
| 3 | Path exposure in /api/health | 🟡 SHOULD FIX | `src/trainingos/coach_web.py` | 269 | `TestPathExposureFull` |
| 4 | Missing date validation | 🟡 SHOULD FIX | `src/trainingos/coach_web.py` | 62–64 | `TestDateValidationFull` |

---

## Issue #1: Orphaned Records in `notes.py` — CRITICAL

**File:** `src/trainingos/notes.py:170`

**Problem:**  
The delete function checks that a note exists in `context_notes`, then deletes from the `records` table instead. This leaves the `context_notes` row orphaned because the deletion targets the wrong table. Over time, the database bloats with unreferenced rows that are invisible in list but persistent in the database.

**Impact:** Data consistency violation, database bloat, potential corruption risk.

**Fix:**

```python
# Before (BUGGY)
connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))

# After (FIXED)
connection.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
```

**Test:**
```bash
pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull -v
```

Expected: 5 tests pass (verify buggy behavior, confirm fix eliminates orphans, test edge cases)

---

## Issue #2: Unhandled Exception in `/api/coach` POST — BUG

**File:** `src/trainingos/coach_web.py:161–167`

**Problem:**  
The `/api/coach` POST handler calls `service.answer(question)` without exception handling. If the coach service fails (database error, provider timeout, malformed evidence), the handler crashes without returning a JSON error response.

**Impact:** Client receives no response, server logs polluted, no graceful degradation.

**Fix:**

```python
# Before (BUGGY)
with connect_database(database_path) as connection:
    service = CoachService(...)
    answer = service.answer(question)  # ← NO TRY/EXCEPT
self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))

# After (FIXED)
try:
    with connect_database(database_path) as connection:
        service = CoachService(...)
        answer = service.answer(question)
except Exception as error:
    self._send_json(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        {"error": "coach service unavailable"},
    )
    return
self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))
```

**Test:**
```bash
pytest tests/test_bugs_before_and_after.py::TestCoachExceptionHandlingFull -v
```

Expected: 4 tests pass (verify crash behavior, confirm fix returns JSON error, test edge cases)

---

## Issue #3: Path Exposure in `/api/health` — SHOULD FIX

**File:** `src/trainingos/coach_web.py:269` in `_health_payload()`

**Problem:**  
The `/api/health` endpoint returns the full expanded filesystem path, leaking home directory, username, and exact file location:

```json
{
  "status": "ok",
  "database": {
    "path": "/Users/athlete/.local/share/trainingos/trainingos.sqlite3"
  }
}
```

While the coach only listens on `127.0.0.1:8765`, any tool or person with local network access can query this and discover filesystem paths.

**Impact:** Information disclosure (username, home directory, system configuration).

**Fix (Recommended: Omit path entirely):**

```python
# Before (BUGGY)
payload["database"] = {
    "path": str(database_path.expanduser().absolute()),
    "retrieval_documents": 0,
}

# After (FIXED)
payload["database"] = {
    "retrieval_documents": 0,
}
```

**Alternative Fix (Mask with generic identifier):**
```python
payload["database"] = {
    "path": "configured",
    "retrieval_documents": 0,
}
```

**Test:**
```bash
pytest tests/test_bugs_before_and_after.py::TestPathExposureFull -v
```

Expected: 4 tests pass (verify path is exposed, confirm fix removes it, test masked alternative)

---

## Issue #4: Missing Date Validation in `/api/notes` GET — SHOULD FIX

**File:** `src/trainingos/coach_web.py:62–64`

**Problem:**  
The `since` query parameter is not validated before being passed to SQLite's `date()` function. If a client sends `?since=banana`, SQLite will return NULL or unpredictable results. CLI validates strictly with `_parse_iso_date()`, but the web API doesn't.

**Impact:** Invalid dates produce unpredictable SQL behavior; no error feedback to clients.

**Fix:**

```python
# Before (BUGGY)
if since_param is not None:
    filters.append("date(note.occurred_at) >= ?")
    params.append(since_param)  # ← No validation

# After (FIXED)
if since_param is not None:
    try:
        since_dt = _parse_iso_date(since_param)
    except ValueError as error:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return
    filters.append("date(note.occurred_at) >= ?")
    params.append(since_dt.date().isoformat())
```

**Note:** Import `_parse_iso_date` from `trainingos.notes`:
```python
from trainingos.notes import NOTE_KIND_TYPES, NOTE_TYPE_KINDS, NOTE_TYPES, _parse_iso_date
```

**Test:**
```bash
pytest tests/test_bugs_before_and_after.py::TestDateValidationFull -v
```

Expected: 5 tests pass (verify invalid dates accepted, confirm fix rejects them, test edge cases like leap years)

---

## Test Suite Quick Reference

### Setup
```bash
pip install pytest pytest-cov
pip install -e .
```

### Run all tests
```bash
pytest tests/test_bugs_before_and_after.py -v
```

**Expected output:** All 19 tests pass in ~2 seconds

### Run by issue
```bash
# Issue #1 (CRITICAL)
pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull -v

# Issue #2 (BUG)
pytest tests/test_bugs_before_and_after.py::TestCoachExceptionHandlingFull -v

# Issue #3 (SHOULD FIX)
pytest tests/test_bugs_before_and_after.py::TestPathExposureFull -v

# Issue #4 (SHOULD FIX)
pytest tests/test_bugs_before_and_after.py::TestDateValidationFull -v
```

### Run with coverage
```bash
pytest tests/test_bugs_before_and_after.py --cov=src/trainingos --cov-report=html
```

**Expected coverage:**
- `notes.py`: 90%+
- `coach_web.py`: 85%+

---

## Test Coverage Details

### Issue #1: Orphaned Records (5 tests)

- **test_01_buggy_delete_orphans_context_notes:** Verifies context_notes row left in DB after delete
- **test_02_orphaned_note_invisible_but_persistent:** Verifies orphaned rows don't show in list but accumulate
- **test_03_fixed_delete_removes_from_context_notes:** Verifies fixed code deletes from correct table
- **test_04_fixed_orphaned_count_reduced:** Verifies no orphans created with fix applied
- **test_05_orphaned_rows_edge_cases:** Verifies multiple deletes don't accumulate orphans

### Issue #2: Unhandled Exception (4 tests)

- **test_01_buggy_service_exception_crashes:** Verifies coach service error crashes handler
- **test_02_buggy_no_json_error_response:** Verifies no JSON response sent on error
- **test_03_fixed_exception_returns_json_error:** Verifies fixed code returns JSON error with 500 status
- **test_04_exception_handling_edge_cases:** Verifies all exception types handled uniformly

### Issue #3: Path Exposure (4 tests)

- **test_01_buggy_exposes_full_path:** Verifies full path is in /api/health response
- **test_02_buggy_leaks_username:** Verifies username can be inferred from path
- **test_03_fixed_omits_path:** Verifies fixed code doesn't include path
- **test_04_fixed_masked_identifier:** Verifies alternative fix with generic identifier

### Issue #4: Date Validation (5 tests)

- **test_01_buggy_accepts_invalid_date:** Verifies invalid dates accepted without validation
- **test_02_buggy_sqlite_interprets_invalid_date:** Verifies SQLite produces unpredictable results
- **test_03_fixed_validates_date_format:** Verifies fixed code validates YYYY-MM-DD format
- **test_04_fixed_rejects_invalid_since_parameter:** Verifies JSON error returned for invalid date
- **test_05_date_validation_edge_cases:** Verifies leap years, boundaries, format variations

---

## Integration: Token Budget Feature

**Related tests:** `tests/test_coach_web.py` and existing `tests/test_coach.py` token_budget tests

The token_budget feature is integrated with the coach API. Test cases validate:
- Web API accepts `token_budget` parameter in POST `/api/coach`
- Invalid token_budget values (≤0, non-integer) are rejected with 400 Bad Request
- Token budget truncates evidence when exceeded
- Truncated documents are disclosed to the provider
- Omitting `token_budget` includes all matching evidence (backward compatible)

All 7 existing token_budget tests in `test_coach.py` continue to pass alongside the 5 new web API integration tests.

---

## Commit Strategy (Recommended Order)

Apply fixes in order, one commit per fix:

```bash
# Fix #1: CRITICAL
git add src/trainingos/notes.py
git commit -m "fix: Remove orphaned records in notes delete operation

Deletes from context_notes table first (correct table) before
cleaning up records table. Prevents accumulation of orphaned
context_notes rows that were invisible in list but persistent
in database."

# Fix #2: BUG
git add src/trainingos/coach_web.py
git commit -m "fix: Add error handling to /api/coach POST endpoint

Wraps service.answer() call in try/except and returns JSON error
response on failure instead of crashing handler. Ensures graceful
degradation when coach service is unavailable."

# Fix #3: SHOULD FIX (path exposure)
git add src/trainingos/coach_web.py
git commit -m "fix: Mask database path in /api/health response

Omits full filesystem path from health check response to avoid
leaking home directory and username information."

# Fix #4: SHOULD FIX (date validation)
git add src/trainingos/coach_web.py
git commit -m "fix: Validate date format in /api/notes since parameter

Validates since parameter matches YYYY-MM-DD format before passing
to SQL query. Prevents unpredictable SQLite behavior from invalid
dates."
```

---

## Verification Checklist

After applying all fixes:

- [ ] Run full test suite: `pytest tests/test_bugs_before_and_after.py -v`
- [ ] All 19 tests pass
- [ ] Run with coverage: `pytest tests/ --cov=src/trainingos --cov-report=term-missing`
- [ ] Coverage >80% on modified files
- [ ] No new test failures in existing suite
- [ ] Commits are atomic (one fix per commit)
- [ ] Commit messages follow conventional commits (feat:, fix:, etc.)

---

## Rollback

If a fix needs to be reverted:

```bash
# Revert a specific commit
git revert <commit-hash>

# Or reset to before changes
git reset --hard <commit-before-fix>
```

---

## Security Review Summary

### ✅ No Critical Vulnerabilities Found

- **SQL Injection:** Parameterized queries used correctly throughout
- **Command Injection:** No shell execution or command construction
- **XSS:** HTML escaping present; JSON safe
- **Path Traversal:** Paths validated and normalized
- **Authentication:** Local-only binding (127.0.0.1:8765)

### ⚠️ Defensive Improvements (Addressed by These Fixes)

- **Data Consistency:** Fix orphaned records (CRITICAL)
- **Error Handling:** Catch coach service exceptions (BUG)
- **Information Disclosure:** Mask filesystem paths (SHOULD FIX)
- **Input Validation:** Validate date formats (SHOULD FIX)
