# Token Budget Feature - Test Cases

## Overview
These test cases were created to validate the code review findings for the token_budget feature integration. They cover:

1. **Web API Integration** - Token budget parameter handling in the coach API
2. **Parameter Validation** - Input validation for token_budget values
3. **Functionality** - Token budget effect on evidence selection and truncation
4. **Disclosure** - Proper caveat and omitted document reporting

## Test Cases

### New Web API Tests (test_coach_web.py)

#### `test_api_accepts_token_budget_parameter`
- **Purpose**: Verify the web API accepts and processes the `token_budget` parameter
- **Scenario**: POST to `/api/coach` with `token_budget: 50`
- **Assertion**: Request succeeds and provider is called

#### `test_api_validates_token_budget_parameter`
- **Purpose**: Ensure invalid `token_budget` values are rejected
- **Scenarios**:
  - `token_budget: 0` → error (must be positive)
  - `token_budget: -10` → error (must be positive)
  - `token_budget: "not_an_int"` → error (must be integer)
- **Assertion**: All invalid cases return 400 Bad Request with appropriate error message

#### `test_api_token_budget_truncates_evidence_when_exceeded`
- **Purpose**: Verify that token_budget actually limits evidence inclusion
- **Setup**: 3 large retrieval documents (~600 tokens each)
- **Scenario**: POST with `token_budget: 100` on "weekly distance" query
- **Assertion**: Fewer than 3 documents are returned, truncation caveat is present

#### `test_api_token_budget_discloses_omitted_documents_in_prompt`
- **Purpose**: Verify that truncated documents are disclosed to the provider
- **Setup**: 3 large retrieval documents with token budget insufficient for all
- **Scenario**: POST with `token_budget: 80`
- **Assertions**:
  - Provider is called (evidence exists)
  - Prompt includes "Omitted:" listing
  - Prompt includes "More matching local documents existed but were omitted by budget"

#### `test_api_token_budget_none_includes_all_evidence`
- **Purpose**: Verify that omitting `token_budget` includes all matching evidence
- **Setup**: 3 retrieval documents
- **Scenario**: POST without `token_budget` parameter
- **Assertions**:
  - All 3+ matching documents are included
  - No truncation caveat in response

### Existing Tests (Still Pass)

All 7 existing `token_budget` tests in `test_coach.py` continue to pass:
- `test_broad_race_readiness_question_searches_full_history_within_token_budget`
- `test_ambiguous_question_fallback_respects_token_budget`
- `test_token_budget_overflow_truncates_evidence_and_discloses_counts`
- `test_provider_prompt_respects_token_budget_ceiling`
- `test_zero_token_budget_raises_at_construction`
- `test_token_budget_single_document_exact_fit_includes_it`
- `test_missing_data_returns_insufficiency_regardless_of_token_budget`

## Code Changes

### coach_web.py
- Added `_optional_token_budget()` validation function
- Updated `do_POST()` to extract `token_budget` from request payload
- Pass `token_budget` to `CoachService` constructor

### Validation Logic
```python
def _optional_token_budget(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("token_budget must be a positive integer")
    if value <= 0:
        raise ValueError("token_budget must be a positive integer")
    return value
```

## Test Coverage

| Finding | Test Case | Status |
|---------|-----------|--------|
| Token budget not in web API | test_api_accepts_token_budget_parameter | ✓ |
| Token budget validation | test_api_validates_token_budget_parameter | ✓ |
| Token budget effect on evidence | test_api_token_budget_truncates_evidence_when_exceeded | ✓ |
| Omitted documents disclosure | test_api_token_budget_discloses_omitted_documents_in_prompt | ✓ |
| Backward compatibility (no budget) | test_api_token_budget_none_includes_all_evidence | ✓ |
| Service-level token budget | All existing coach.py tests | ✓ |

## Remaining Gaps

While the web API integration is now tested, the following are **not yet configurable**:
- Token budget in AppConfig / environment variables
- Token budget as a default system setting
- Token budget documentation in API

These would require:
- Adding `TRAININGOS_TOKEN_BUDGET` env var
- Adding `token_budget` field to AppConfig
- Exposing configuration in health API
- Client-side documentation

## Test Execution

Run all token_budget tests:
```bash
PYTHONPATH=src python3 -m unittest discover tests -k token_budget -v
```

Run full test suite:
```bash
PYTHONPATH=src python3 -m unittest discover tests -v
```

**Status**: All 161 tests pass ✓
