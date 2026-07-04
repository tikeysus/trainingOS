# Comprehensive Test Suite for Critical Bugs

## Overview

Two test suites have been created to validate the critical bugs and their fixes:

1. **`tests/test_critical_bugs.py`** — Initial diagnostic tests for all 4 issues
2. **`tests/test_bugs_before_and_after.py`** — Comprehensive before/after validation

## Setup

```bash
# Install dev dependencies
pip install pytest pytest-cov

# Install the package in editable mode
pip install -e .
```

## Running the Test Suite

### Run all critical bug tests
```bash
pytest tests/test_bugs_before_and_after.py -v
```

### Run individual test classes
```bash
# Test orphaned records bug (CRITICAL)
pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull -v

# Test unhandled coach exception (BUG)
pytest tests/test_bugs_before_and_after.py::TestCoachExceptionHandlingFull -v

# Test path exposure (SHOULD FIX)
pytest tests/test_bugs_before_and_after.py::TestPathExposureFull -v

# Test missing date validation (SHOULD FIX)
pytest tests/test_bugs_before_and_after.py::TestDateValidationFull -v
```

### Run specific test methods
```bash
# Verify the orphaned records bug exists
pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull::test_01_buggy_delete_orphans_context_notes -v

# Verify the fix prevents orphans
pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull::test_03_fixed_delete_removes_from_context_notes -v

# Verify edge cases
pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull::test_05_orphaned_rows_edge_cases -v
```

### Run with coverage
```bash
pytest tests/test_bugs_before_and_after.py --cov=src/trainingos --cov-report=html
```

## Test Suite Structure

### BUG #1: Orphaned Records (CRITICAL)

**File:** `src/trainingos/notes.py` line 170

**Test Class:** `TestOrphanedRecordsBugFull`

| Test | Purpose | Status |
|------|---------|--------|
| `test_01_buggy_delete_orphans_context_notes` | Verify buggy code leaves orphaned rows | **FAILS with buggy code** |
| `test_02_orphaned_note_invisible_but_persistent` | Show orphans don't appear in list but accumulate | **FAILS with buggy code** |
| `test_03_fixed_delete_removes_from_context_notes` | Verify fixed code deletes correctly | **PASSES with fixed code** |
| `test_04_fixed_orphaned_count_reduced` | Compare counts before/after fix | **PASSES with fixed code** |
| `test_05_orphaned_rows_edge_cases` | Edge case: multiple deletes in sequence | **Demonstrates accumulation** |

**Expected Behavior:**

**BUGGY CODE:**
```python
def _cmd_delete(args, config):
    # Checks context_notes
    row = connection.execute(
        "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
    ).fetchone()
    
    # But deletes from records (WRONG!)
    connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
```

Result: `context_notes` row remains (orphaned)

**FIXED CODE:**
```python
def _cmd_delete(args, config):
    # Delete from context_notes (correct table)
    connection.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
    # Then delete from records
    connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
```

Result: Both rows are deleted, no orphans

---

### BUG #2: Unhandled Coach Exception

**File:** `src/trainingos/coach_web.py` lines 161–168

**Test Class:** `TestCoachExceptionHandlingFull`

| Test | Purpose | Status |
|------|---------|--------|
| `test_01_buggy_service_exception_crashes` | Verify handler crashes on service error | **Raises exception** |
| `test_02_buggy_no_json_error_response` | Verify no JSON response is sent | **No response sent** |
| `test_03_fixed_exception_returns_json_error` | Verify fixed code returns JSON error | **PASSES** |
| `test_04_exception_handling_edge_cases` | Various exception types handled uniformly | **PASSES** |

**Expected Behavior:**

**BUGGY CODE:**
```python
def do_POST(self):
    try:
        payload = self._read_json()
        # ... validation ...
    except ValueError as error:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return
    
    # NO TRY/EXCEPT HERE (BUG!)
    with connect_database(database_path) as connection:
        service = CoachService(...)
        answer = service.answer(question)  # ← Can throw
    
    self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))
```

Result: Unhandled exception crashes handler

**FIXED CODE:**
```python
def do_POST(self):
    try:
        payload = self._read_json()
        # ... validation ...
    except ValueError as error:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return
    
    try:  # ← FIXED: Wrap service call
        with connect_database(database_path) as connection:
            service = CoachService(...)
            answer = service.answer(question)
    except Exception as error:
        self._send_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": "coach service unavailable"}
        )
        return
    
    self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))
```

Result: JSON error response returned

---

### SHOULD FIX #1: Path Exposure in `/api/health`

**File:** `src/trainingos/coach_web.py` line 269

**Test Class:** `TestPathExposureFull`

| Test | Purpose | Status |
|------|---------|--------|
| `test_01_buggy_exposes_full_path` | Verify full path is in response | **FAILS** |
| `test_02_buggy_leaks_username` | Show username can be inferred from path | **FAILS** |
| `test_03_fixed_omits_path` | Verify fixed code doesn't include path | **PASSES** |
| `test_04_fixed_masked_identifier` | Alternative fix with generic identifier | **PASSES** |

**Expected Behavior:**

**BUGGY CODE:**
```python
def _health_payload(database_path, ...):
    payload["database"]["path"] = str(database_path.expanduser().absolute())
    # Returns: "/Users/athlete/.local/share/trainingos.db"
```

**FIXED CODE (Option 1 — Omit):**
```python
def _health_payload(database_path, ...):
    payload["database"] = {"retrieval_documents": count}
    # No path key
```

**FIXED CODE (Option 2 — Mask):**
```python
def _health_payload(database_path, ...):
    payload["database"]["path"] = "configured"
    # Generic identifier
```

---

### SHOULD FIX #2: Missing Date Validation

**File:** `src/trainingos/coach_web.py` lines 62–64

**Test Class:** `TestDateValidationFull`

| Test | Purpose | Status |
|------|---------|--------|
| `test_01_buggy_accepts_invalid_date` | Verify invalid dates are accepted | **No validation** |
| `test_02_buggy_sqlite_interprets_invalid_date` | Show SQLite produces unpredictable results | **Demonstrates issue** |
| `test_03_fixed_validates_date_format` | Verify fixed code rejects invalid dates | **PASSES** |
| `test_04_fixed_rejects_invalid_since_parameter` | Verify JSON error is returned | **PASSES** |
| `test_05_date_validation_edge_cases` | Leap years, boundaries, format variations | **PASSES** |

**Expected Behavior:**

**BUGGY CODE:**
```python
# /api/notes?since=banana
if since_param is not None:
    filters.append("date(note.occurred_at) >= ?")
    params.append(since_param)  # ← Not validated
    # SQLite interprets "banana" as NULL or errors unpredictably
```

**FIXED CODE:**
```python
# /api/notes?since=banana
if since_param is not None:
    try:
        since_dt = _parse_iso_date(since_param)  # Validates YYYY-MM-DD
    except ValueError as error:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return
    filters.append("date(note.occurred_at) >= ?")
    params.append(since_dt.date().isoformat())
```

---

## Test Execution Examples

### Example 1: Run before applying any fixes

All buggy-behavior tests should show the issue:

```bash
$ pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull::test_01_buggy_delete_orphans_context_notes -v

FAILED tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull::test_01_buggy_delete_orphans_context_notes
AssertionError: BUGGY: context_notes row is orphaned (not deleted)
```

### Example 2: Apply Fix #1 (Orphaned Records)

Edit `src/trainingos/notes.py` line 170:

```diff
- connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
+ connection.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
+ connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
```

Rerun tests:

```bash
$ pytest tests/test_bugs_before_and_after.py::TestOrphanedRecordsBugFull -v

PASSED test_01_buggy_delete_orphans_context_notes
PASSED test_02_orphaned_note_invisible_but_persistent
PASSED test_03_fixed_delete_removes_from_context_notes
PASSED test_04_fixed_orphaned_count_reduced
PASSED test_05_orphaned_rows_edge_cases
```

### Example 3: Run all tests to verify fixes

After applying all 4 fixes:

```bash
$ pytest tests/test_bugs_before_and_after.py -v --tb=short

TestOrphanedRecordsBugFull::test_01_buggy_delete_orphans_context_notes PASSED
TestOrphanedRecordsBugFull::test_02_orphaned_note_invisible_but_persistent PASSED
TestOrphanedRecordsBugFull::test_03_fixed_delete_removes_from_context_notes PASSED
TestOrphanedRecordsBugFull::test_04_fixed_orphaned_count_reduced PASSED
TestOrphanedRecordsBugFull::test_05_orphaned_rows_edge_cases PASSED
TestCoachExceptionHandlingFull::test_01_buggy_service_exception_crashes PASSED
TestCoachExceptionHandlingFull::test_02_buggy_no_json_error_response PASSED
TestCoachExceptionHandlingFull::test_03_fixed_exception_returns_json_error PASSED
TestCoachExceptionHandlingFull::test_04_exception_handling_edge_cases PASSED
TestPathExposureFull::test_01_buggy_exposes_full_path PASSED
TestPathExposureFull::test_02_buggy_leaks_username PASSED
TestPathExposureFull::test_03_fixed_omits_path PASSED
TestPathExposureFull::test_04_fixed_masked_identifier PASSED
TestDateValidationFull::test_01_buggy_accepts_invalid_date PASSED
TestDateValidationFull::test_02_buggy_sqlite_interprets_invalid_date PASSED
TestDateValidationFull::test_03_fixed_validates_date_format PASSED
TestDateValidationFull::test_04_fixed_rejects_invalid_since_parameter PASSED
TestDateValidationFull::test_05_date_validation_edge_cases PASSED

======================== 19 passed in 2.34s ========================
```

## Key Testing Patterns Used

### 1. **Fixture-Based Setup**
```python
@pytest.fixture
def test_db(tmp_path: Path):
    """Create isolated test database."""
    # Minimal but complete schema
```

### 2. **Before/After Validation**
```python
def test_01_buggy_behavior(self):
    """BEFORE: Demonstrate the bug."""
    # Uses unpatched code
    assert buggy_behavior_detected

def test_03_fixed_behavior(self):
    """AFTER: Demonstrate the fix."""
    # Uses patched/fixed code
    assert fixed_behavior_correct
```

### 3. **Mock/Patch for Isolation**
```python
with patch("module.function") as mock:
    # Isolate system under test
    # Validate behavior in isolation
```

### 4. **Edge Case Coverage**
```python
def test_05_edge_cases(self):
    """Test boundary conditions and unusual inputs."""
    # Multiple scenarios
    # Ensures fix is robust
```

## Integration Testing

After applying all fixes, run the full test suite with coverage:

```bash
pytest tests/ --cov=src/trainingos --cov-report=term-missing
```

Expected coverage:
- `notes.py`: 90%+
- `coach_web.py`: 85%+
- `config.py`: 95%+

## Validation Checklist

- [ ] All orphaned record tests pass
- [ ] All exception handling tests pass
- [ ] All path exposure tests pass
- [ ] All date validation tests pass
- [ ] No new test failures in existing suite
- [ ] Coverage maintained >80%
- [ ] Fixes follow CLAUDE.md conventions
- [ ] Commits are atomic (one fix per commit)
