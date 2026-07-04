# Quick Reference: All Fixes

## FIX #1: Orphaned Records in `notes.py` (CRITICAL)

**File:** `src/trainingos/notes.py`  
**Line:** 170  
**Severity:** CRITICAL — Data accumulates, consistency violated

```diff
def _cmd_delete(args: argparse.Namespace, config: AppConfig) -> None:
    note_id = args.note_id
    with connect_database(config.database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            sys.stderr.write(f"note not found: {note_id!r}\n")
            sys.exit(1)
-       connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
+       # Delete from context_notes first (correct table)
+       connection.execute("DELETE FROM context_notes WHERE record_id = ?", (note_id,))
+       # Then clean up records table
+       connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
        connection.commit()
```

**Test:** `test_bugs_before_and_after.py::TestOrphanedRecordsBugFull`

---

## FIX #2: Unhandled Exception in `/api/coach` (BUG)

**File:** `src/trainingos/coach_web.py`  
**Lines:** 161–167  
**Severity:** BUG — Handler crashes, no error response

```diff
        try:
            payload = self._read_json()
            question = _required_text(payload.get("question"), "question")
            evidence_limit = _optional_evidence_limit(payload.get("evidence_limit"))
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
+       try:
            with connect_database(database_path) as connection:
                service = CoachService(
                    connection,
                    provider,
                    evidence_limit=evidence_limit or DEFAULT_EVIDENCE_LIMIT,
                )
                answer = service.answer(question)
+       except Exception as error:
+           self._send_json(
+               HTTPStatus.INTERNAL_SERVER_ERROR,
+               {"error": "coach service unavailable"},
+           )
+           return
        self._send_json(HTTPStatus.OK, coach_answer_to_json(answer))
```

**Test:** `test_bugs_before_and_after.py::TestCoachExceptionHandlingFull`

---

## FIX #3: Path Exposure in `/api/health` (SHOULD FIX)

**File:** `src/trainingos/coach_web.py`  
**Line:** 269  
**Severity:** SHOULD FIX — Leaks filesystem paths and username

### Option A: Omit path entirely (Recommended)

```diff
def _health_payload(
    database_path: Path,
    provider_health: Callable[[], OllamaHealth] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "database": {
-           "path": str(database_path.expanduser().absolute()),
            "retrieval_documents": 0,
        },
        "provider": {"available": None},
    }
```

### Option B: Use masked identifier

```diff
def _health_payload(
    database_path: Path,
    provider_health: Callable[[], OllamaHealth] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "database": {
-           "path": str(database_path.expanduser().absolute()),
+           "path": "configured",
            "retrieval_documents": 0,
        },
        "provider": {"available": None},
    }
```

**Test:** `test_bugs_before_and_after.py::TestPathExposureFull`

---

## FIX #4: Missing Date Validation (SHOULD FIX)

**File:** `src/trainingos/coach_web.py`  
**Lines:** 62–64  
**Severity:** SHOULD FIX — Invalid dates produce unpredictable SQL behavior

```diff
        if type_param is not None and type_param in NOTE_TYPE_KINDS:
            filters.append("note.note_kind = ?")
            params.append(NOTE_TYPE_KINDS[type_param].value)
        if since_param is not None:
+           try:
+               since_dt = _parse_iso_date(since_param)
+           except ValueError as error:
+               self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
+               return
            filters.append("date(note.occurred_at) >= ?")
-           params.append(since_param)
+           params.append(since_dt.date().isoformat())
```

**Note:** Import `_parse_iso_date` at the top of `coach_web.py`:

```diff
from trainingos.notes import NOTE_KIND_TYPES, NOTE_TYPE_KINDS, NOTE_TYPES
+ from trainingos.notes import NOTE_KIND_TYPES, NOTE_TYPE_KINDS, NOTE_TYPES, _parse_iso_date
```

Wait, it's already imported. Just add `_parse_iso_date`:

```diff
- from trainingos.notes import NOTE_KIND_TYPES, NOTE_TYPE_KINDS, NOTE_TYPES
+ from trainingos.notes import NOTE_KIND_TYPES, NOTE_TYPE_KINDS, NOTE_TYPES, _parse_iso_date
```

**Test:** `test_bugs_before_and_after.py::TestDateValidationFull`

---

## OPTIONAL: API Consistency (EDGE CASE)

**File:** `src/trainingos/coach_web.py`  
**Lines:** 139–145  
**Severity:** EDGE CASE — CLI supports activity linking, web doesn't

Only implement if activity linking is desired for web API.

```diff
                try:
                    note_type = payload.get("type")
                    if not isinstance(note_type, str) or note_type not in NOTE_TYPE_KINDS:
                        raise ValueError(
                            f"type must be one of: {', '.join(NOTE_TYPES)}"
                        )
                    body = payload.get("body")
                    if not isinstance(body, str) or not body.strip():
                        raise ValueError("body must be a non-blank string")
                    date_raw = payload.get("date")
                    if date_raw is not None:
                        try:
                            occurred_at = datetime.strptime(date_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                        except (ValueError, TypeError):
                            raise ValueError(
                                f"date must be in YYYY-MM-DD format, got: {date_raw!r}"
                            )
                    else:
                        today = datetime.now(UTC).date()
                        occurred_at = datetime(today.year, today.month, today.day, tzinfo=UTC)
+                   activity_id = payload.get("activity_id")
+                   linked_record_ids = ()
+                   if activity_id is not None:
+                       if not isinstance(activity_id, str):
+                           raise ValueError("activity_id must be a string")
+                       # Validate activity exists
+                       activity_row = connection.execute(
+                           "SELECT 1 FROM activities WHERE record_id = ?",
+                           (activity_id,),
+                       ).fetchone()
+                       if activity_row is None:
+                           raise ValueError(f"activity not found: {activity_id!r}")
+                       linked_record_ids = (activity_id,)
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                record_id = str(uuid.uuid4())
                now = datetime.now(UTC)
                metadata = RecordMetadata(
                    record_id=record_id,
                    timezone="UTC",
                    created_at=now,
                    updated_at=now,
                    provenance=Provenance(ProvenanceKind.USER_ENTERED),
                )
                note = ContextNote(
                    metadata=metadata,
                    occurred_at=occurred_at,
                    kind=NOTE_TYPE_KINDS[note_type],
                    text=body.strip(),
-                   linked_record_ids=(),
+                   linked_record_ids=linked_record_ids,
                )
```

---

## Commit Strategy (Recommended)

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

## Verification

After applying all fixes:

```bash
# Run test suite
pytest tests/test_bugs_before_and_after.py -v

# Expected: All 19 tests pass
# Verify coverage
pytest tests/ --cov=src/trainingos --cov-report=term-missing

# Expected: >85% coverage on modified files
```

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

## Before/After Comparison

| Issue | Before | After |
|-------|--------|-------|
| **Orphaned Records** | context_notes rows remain after delete | Both context_notes and records are deleted |
| **Coach Exception** | Handler crashes, no response | JSON error returned with 500 status |
| **Path Exposure** | Full path leaked: `/Users/athlete/.local/...` | Path omitted or generic identifier used |
| **Date Validation** | Invalid dates silently interpreted by SQLite | Invalid dates rejected with 400 error |
