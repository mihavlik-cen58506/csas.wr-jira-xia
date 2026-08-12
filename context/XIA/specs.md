# XIA Nightly Processor - Keboola Custom Component Specification

Version: 1.0
Owner: DaTest team
Date: 2026-06-23
Target audience: Component-generation agent and Keboola component developers

## 1. Purpose

Build a Keboola custom component that replaces the current Python Transformation logic in [context/XIA/xia_nightly_main.py](context/XIA/xia_nightly_main.py).

Business goal:
- Process all pending XIA archive requests during nightly schedule.
- Revalidate queued Jira issues.
- Bulk move eligible issues to XIA project.
- Persist outcomes back to XIA tables.

Reason for component:
- Company policy disallows third-party API calls from Keboola Transformations.
- Third-party calls must run from a dedicated custom component.

## 2. Scope

In scope (V1):
- Operation type: ARCHIVE_TEST only.
- Read and write exactly 3 semantic tables resolved from mapping by name match:
  - table name contains XIA_REQUESTS
  - table name contains XIA_REQUEST_ITEMS
  - table name contains XIA_RUNS
- Jira integration via REST API v3:
  - Search issues by JQL
  - Bulk move issues
  - Poll task status

Out of scope (V2, do not implement now):
- DELETE_TEST
- DELETE_TEST_EXECUTION
- MOVE_STATUS / DELETE_STATUS activation logic
- Restore metadata custom fields

## 3. Component Type and Skeleton

Use standard Keboola Python component template (cookiecutter-python-component style).

Required implementation layout:
- component_config/
  - configSchema.json
  - configRowSchema.json
  - component_short_description.md
  - component_long_description.md
- src/
  - component.py (entrypoint)
  - configuration.py (parameter validation)
  - jira_api.py (Jira client)
  - processor.py (business orchestration)
  - csv_io.py (table read/write helpers)
  - constants.py (status and skip reason constants)

## 4. Runtime Contract

### 4.1 Configuration parameters (exactly these 2)

Component must expose exactly these required parameters:
- JIRA_BASE_URL (Jira API gateway base URL, e.g. https://api.atlassian.com/ex/jira/{cloud_id})
- JIRA_API_TOKEN (service account API token)

Security requirement:
- JIRA_API_TOKEN must be encrypted in Keboola UI schema.
- In source configuration model use alias with hash for encrypted params, e.g. #jira_api_token.

### 4.2 Input mapping

Component expects exactly 3 mapped input tables:
- one table matching XIA_REQUESTS
- one table matching XIA_REQUEST_ITEMS
- one table matching XIA_RUNS

Important:
- No extra table-name parameters are used in component config.
- Physical table names are resolved from Keboola Input Mapping at runtime.
- Names can vary across environments/configurations (suffixes/prefixes are allowed).

Table role resolution strategy:
- Component resolves roles by case-insensitive name match on mapped table name.
- Matching rules (contains):
  - REQUEST_ITEMS role: XIA_REQUEST_ITEMS
  - REQUESTS role: XIA_REQUESTS
  - RUNS role: XIA_RUNS
- Each role must be matched exactly once.
- If any role is missing or ambiguous, component fails fast with clear UserException.
- As safety check, matched tables must still contain required columns from section 5.

### 4.3 Output mapping

Component writes exactly 3 output tables (same semantic roles):
- one table matching XIA_REQUESTS
- one table matching XIA_REQUEST_ITEMS
- one table matching XIA_RUNS

Write mode:
- Full replace for all three output tables.

Output mapping rule:
- Component writes outputs to destinations defined in Keboola Output Mapping.
- Component must not hardcode physical table names.

## 5. Data Requirements

### 5.1 Required columns in REQUESTS table (name contains XIA_REQUESTS)

Must exist:
- KEY
- REQUEST_REF
- OPERATION_TYPE
- STATUS
- UPDATED_BY
- UPDATED_DATETIME

### 5.2 Required columns in REQUEST_ITEMS table (name contains XIA_REQUEST_ITEMS)

Must exist:
- XIA_REQUEST_KEY
- ISSUE_ID
- SOURCE_SPACE_KEY
- ITEM_STATUS
- ERROR_CODE
- ERROR_MESSAGE
- EXECUTED_DATETIME
- UPDATED_BY
- UPDATED_DATETIME

### 5.3 Required columns in RUNS table (name contains XIA_RUNS)

Must exist (for append rows):
- XIA_RUN_KEY
- XIA_REQUEST_KEY
- RUN_SEQUENCE
- RUN_TYPE
- RUN_STATUS
- STARTED_DATETIME
- FINISHED_DATETIME
- ITEMS_TOTAL_COUNT
- ITEMS_SUCCESS_COUNT
- ITEMS_FAILED_COUNT
- ITEMS_SKIPPED_COUNT
- ERROR_SUMMARY
- INSERTED_BY
- INSERTED_DATETIME
- UPDATED_BY
- UPDATED_DATETIME

### 5.4 Pass-through rule

For all three tables:
- Preserve all original columns and values not explicitly updated by this component.
- Ignore unknown extra columns safely.

## 6. Processing Logic (Authoritative)

### 6.1 Constants

Use these fixed constants:
- XIA_PROJECT_KEY = XIA
- XIA_ISSUE_TYPE_TEST_ID = 10005
- XIA_NIGHTLY_USER = xia_nightly
- JQL_CHUNK_SIZE = 100

Skip reasons:
- issue_not_found
- wrong_issue_type
- wrong_space
- already_in_xia

### 6.2 Timezone

All generated timestamps must be in Europe/Prague timezone formatted as:
- YYYY-MM-DD HH:MM:SS

### 6.3 Startup crash recovery

Before processing:
- For each request row where STATUS == RUNNING:
  - set STATUS = PENDING
  - set UPDATED_BY = xia_nightly
  - set UPDATED_DATETIME = now_cz

### 6.4 Pending selection

Process only requests where:
- STATUS == PENDING

### 6.5 Operation routing

For each selected request:
- If OPERATION_TYPE == ARCHIVE_TEST: process normally.
- Else (unknown or deferred type):
  - mark request FAILED
  - write one XIA_RUNS row with RUN_TYPE = EXECUTE and RUN_STATUS = FAILED
  - continue with next request.

### 6.6 ARCHIVE_TEST flow

For current request:

1. Load request items:
- Match by XIA_REQUEST_KEY == request.KEY
- Include only ITEM_STATUS == PENDING

2. If no pending items:
- request.STATUS = FAILED
- append run with counts all zero and error summary No PENDING items found.
- continue

3. Lock request:
- request.STATUS = RUNNING
- update audit fields

4. Revalidation by chunks:
- Build dictionary ISSUE_ID -> item
- For chunks of 100 ISSUE_ID values:
  - JQL: id in (id1,id2,...)
  - call Jira search endpoint
  - For each returned issue:
    - if project key == XIA: mark item SKIPPED (already_in_xia)
    - else if project key != item.SOURCE_SPACE_KEY: mark SKIPPED (wrong_space)
    - else if issue type id != 10005: mark SKIPPED (wrong_issue_type)
    - else keep item as PENDING (eligible)
- Any requested ISSUE_ID not returned by Jira:
  - mark SKIPPED (issue_not_found)

5. Determine eligible list:
- eligible_items = items with ITEM_STATUS still PENDING
- skipped_count = count ITEM_STATUS == SKIPPED

6. If eligible list empty:
- request.STATUS = FAILED
- append run with:
  - total = original pending item count
  - success = 0
  - failed = 0
  - skipped = skipped_count
  - error summary All items skipped during re-validation.
- continue

7. Submit bulk move:
- call Jira bulk move for all eligible ISSUE_ID values to:
  - target project XIA
  - target issue type 10005

8. Poll async task:
- poll interval 5s
- max wait 3600s

9. Apply result:
- If task status COMPLETE:
  - all eligible -> ITEM_STATUS = MOVED
  - moved_count = eligible count
  - failed_move_count = 0
- Else:
  - all eligible -> ITEM_STATUS = FAILED
  - failed_move_count = eligible count
  - moved_count = 0

10. Finalize request and run:
- request.STATUS:
  - SUCCESS if moved_count == total_count
  - PARTIAL_SUCCESS if moved_count > 0 and moved_count < total_count
  - FAILED otherwise
- append one XIA_RUNS row with computed counts and summary

### 6.7 Run status derivation

When inserting XIA_RUNS row:
- RUN_STATUS = SUCCESS if ITEMS_SUCCESS_COUNT == ITEMS_TOTAL_COUNT
- RUN_STATUS = PARTIAL_SUCCESS if ITEMS_SUCCESS_COUNT > 0 and < ITEMS_TOTAL_COUNT
- RUN_STATUS = FAILED if ITEMS_SUCCESS_COUNT == 0

RUN_TYPE value for this component:
- EXECUTE

## 7. Jira API Contract

Authentication:
- Bearer token auth with JIRA_API_TOKEN (service account API token), sent as `Authorization: Bearer {JIRA_API_TOKEN}`

Timeouts:
- 30 seconds for each Jira HTTP request

Endpoints:
- POST /rest/api/3/search/jql
- POST /rest/api/3/bulk/issues/move
- GET /rest/api/3/task/{taskId}

Bulk move payload requirements:
- targetToSourcesMapping key format: XIA,10005
- inferClassificationDefaults = true
- inferFieldDefaults = true
- inferStatusDefaults = true
- inferSubtaskTypeDefault = true
- targetMandatoryFields = []

## 8. Error Handling Requirements

### 8.1 Request-level resilience

Unhandled exception while processing one request must not stop whole job:
- Mark that request FAILED.
- Continue with next request.

### 8.2 Revalidation failure

If Jira revalidation call fails:
- Mark all request pending items FAILED with ERROR_CODE = JIRA_ERROR.
- Mark request FAILED.
- Insert FAILED run row.

### 8.3 Bulk move submission failure

If bulk move request fails:
- Mark all eligible items FAILED with ERROR_CODE = JIRA_ERROR.
- Mark request FAILED.
- Insert FAILED run row.

### 8.4 Polling failure / timeout

If task polling fails or times out:
- Mark all eligible items FAILED.
- Mark request FAILED.
- Insert FAILED run row.

### 8.5 Jira error message parsing

Implement structured Jira error extraction from JSON body:
- errorMessages array
- errors object
- fallback keys: message, errorMessage, error_description, error

Use first available message in top-level summary.

## 9. Logging Requirements

Mandatory logs:
- Job start and finish
- Number of recovered RUNNING requests
- Number of pending requests found
- Per-request start and final result
- Revalidation failures
- Bulk move task id
- Task completion/failure outcome

Never log:
- JIRA_API_TOKEN
- Full Authorization header

## 10. Performance and Limits

- Revalidation batching: exactly 100 ISSUE_ID per JQL call.
- Process requests sequentially (one by one).
- Single request may contain up to 500 items from upstream UI validation.
- Polling cap for one request: 3600 seconds.

## 11. Idempotency and Consistency Rules

- Component is restart-safe for stuck RUNNING requests via startup recovery.
- Terminal request statuses (SUCCESS, PARTIAL_SUCCESS, FAILED, CANCELED) must never be reprocessed.
- Within one run, each pending request must produce at most one new XIA_RUNS row.

## 12. Acceptance Criteria

### AC1 - Empty queue
Given no request with STATUS = PENDING
When component runs
Then it only performs crash recovery and rewrites outputs without creating new XIA_RUNS rows.

### AC2 - Happy path
Given one PENDING ARCHIVE_TEST request with 3 valid eligible items
When Jira bulk move task returns COMPLETE
Then all 3 items become MOVED, request becomes SUCCESS, and one XIA_RUNS row is added with success=3 failed=0 skipped=0.

### AC3 - Partial success via skip
Given one PENDING request with 5 items where 2 fail revalidation and 3 are eligible
When bulk move returns COMPLETE
Then 2 items are SKIPPED, 3 are MOVED, request is PARTIAL_SUCCESS, and run counts are total=5 success=3 failed=0 skipped=2.

### AC4 - Revalidation hard failure
Given one PENDING request
When Jira search call fails
Then all its pending items become FAILED, request becomes FAILED, and one FAILED run row is added.

### AC5 - Bulk move task non-complete
Given eligible items exist
When Jira task ends with FAILED, CANCELLED, CANCEL_REQUESTED, or DEAD
Then all eligible items become FAILED and request is FAILED or PARTIAL_SUCCESS depending on moved count (for this V1 flow moved count should be 0 in this branch).

### AC6 - Startup recovery
Given one request stuck in RUNNING from previous crash
When component starts
Then request is reset to PENDING before main processing selection.

## 13. Deliverables Expected from Generation Agent

1. Component source structure matching section 3.
2. Working Keboola schemas in component_config for exactly 3 parameters.
3. Full implementation of V1 behavior from section 6.
4. Minimal unit tests for:
- request status derivation
- skip reason mapping
- crash recovery
- run count calculation
5. README with local run instructions using KBC_DATADIR.
6. No references to Streamlit internals.

## 14. Suggested configSchema Notes (for UI)

Fields:
- JIRA_BASE_URL: string, required, example https://your-domain.atlassian.net
- JIRA_USERNAME: string, required
- JIRA_API_TOKEN: string, required, encrypted

Validation hints:
- JIRA_BASE_URL must start with https://
- trim trailing slash before use

## 15. Migration Note

This component is a technical replacement for [context/XIA/xia_nightly_main.py](context/XIA/xia_nightly_main.py) and must preserve identical V1 behavior and table side-effects.

Functional parity requirement:
- For the same input CSV snapshots and same Jira API responses, resulting output tables must be equivalent.

Allowed implementation differences versus the transformation script:
- Hardcoded table names in the script are replaced by logical aliases + mapping resolution.
- Script-level variable placeholders ({{...}}) are replaced by component parameters.
- Internal module split (component.py, processor.py, jira_api.py) may differ from single-file script layout.

Not allowed differences:
- Different request/item/run status outcomes for the same inputs and Jira responses.
- Different skip-reason assignment logic.
- Different run count semantics (total/success/failed/skipped).
