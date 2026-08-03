# =============================================================================
# XIA Nightly Transformation — Keboola Python Transformation (Stage 2)
# =============================================================================
# Purpose : Process all PENDING XIA archival requests via Jira bulk-move API.
# Design  : context/XIA_phase_f_plan.md
# Flow    : context/XIA_implementation_plan.md § Phase F
#
# Keboola UI setup:
#   Input mapping  : XIA_REQUESTS, XIA_REQUEST_ITEMS, XIA_RUNS
#                    (source: out.c-PROD_DATEST_TABLES.*)
#   Output mapping : same three tables, mode = Replace
#   Variables      : JIRA_BASE_URL, JIRA_USERNAME, JIRA_API_TOKEN (Protected)
#   Packages       : none (requests is pre-installed)
#   Schedule       : 02:00 CET/CEST nightly
# =============================================================================

import base64
import csv
import datetime
import os
import socket
import time
import uuid
import pytz

import requests
from requests.exceptions import Timeout


# =============================================================================
# Jira API client (inlined from clients/jira_client.py)
# =============================================================================

class JiraApiError(Exception):
    """Raised when a Jira API request fails."""

    def __init__(self, summary, *, status_code=None, path=None, method=None,
                 messages=None, field_errors=None, raw=None):
        super().__init__(summary)
        self.status_code = status_code
        self.path = path
        self.method = method
        self.messages = messages or []
        self.field_errors = field_errors or {}
        self.raw = raw


class JiraTaskTimeoutError(Exception):
    """Raised when a Jira async task does not finish within the polling window."""


def _parse_error_body(body):
    messages = []
    field_errors = {}

    if not isinstance(body, dict):
        if body:
            messages.append(str(body))
        return messages, field_errors

    em = body.get("errorMessages")
    if isinstance(em, list):
        messages.extend(str(m) for m in em if m)

    errs = body.get("errors")
    if isinstance(errs, dict):
        field_errors.update({str(k): str(v) for k, v in errs.items()})
    elif isinstance(errs, list):
        for item in errs:
            if isinstance(item, dict):
                msg = item.get("message") or item.get("errorMessage")
                if msg:
                    messages.append(str(msg))

    if not messages and not field_errors:
        for key in ("message", "errorMessage", "error_description", "error"):
            val = body.get(key)
            if val:
                messages.append(str(val))
                break

    return messages, field_errors


class JiraApiClient:
    """Minimal Jira REST API v3 client for the XIA nightly job."""

    def __init__(self, base_url: str, username: str, api_token: str):
        self._base_url = base_url.rstrip("/")
        credentials = base64.b64encode(f"{username}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = requests.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                timeout=30,
                **kwargs,
            )
        except Timeout:
            raise JiraApiError(
                f"Request to {path} timed out after 30 seconds.",
                path=path, method=method,
                messages=["Network timeout after 30 seconds."],
            )

        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            messages, field_errors = _parse_error_body(body)
            summary = f"HTTP {response.status_code} on {method} {path}"
            if messages:
                summary += f": {messages[0]}"
            raise JiraApiError(
                summary,
                status_code=response.status_code, path=path, method=method,
                messages=messages, field_errors=field_errors, raw=body,
            )

        return response

    def search_issues_by_jql(self, jql: str, fields: list = None,
                              max_pages: int = 50, page_size: int = 100) -> list:
        """Run a JQL search, paginate via nextPageToken, return all issues."""
        fields = fields or ["summary", "issuetype", "project", "status"]
        issues = []
        next_token = None

        for _ in range(max_pages):
            payload = {"jql": jql, "fields": fields, "maxResults": page_size}
            if next_token:
                payload["nextPageToken"] = next_token
            response = self._request("POST", "/rest/api/3/search/jql", json=payload)
            data = response.json()
            issues.extend(data.get("issues", []))
            next_token = data.get("nextPageToken")
            if not next_token or data.get("isLast", False):
                break

        return issues

    def bulk_move_issues(self, issue_ids_or_keys: list, target_project_key: str,
                         target_issue_type_id: str,
                         send_bulk_notification: bool = True) -> str:
        """Submit async bulk-move request. Returns taskId."""
        mapping_key = f"{target_project_key},{target_issue_type_id}"
        payload = {
            "sendBulkNotification": send_bulk_notification,
            "targetToSourcesMapping": {
                mapping_key: {
                    "inferClassificationDefaults": True,
                    "inferFieldDefaults": True,
                    "inferStatusDefaults": True,
                    "inferSubtaskTypeDefault": True,
                    "issueIdsOrKeys": issue_ids_or_keys,
                    "targetMandatoryFields": [],
                }
            },
        }
        response = self._request("POST", "/rest/api/3/bulk/issues/move", json=payload)
        task_id = response.json().get("taskId")
        if not task_id:
            raise JiraApiError("Bulk move response missing taskId.")
        return task_id

    def get_task_status(self, task_id: str) -> dict:
        return self._request("GET", f"/rest/api/3/task/{task_id}").json()

    def wait_for_task(self, task_id: str, poll_interval: float = 5.0,
                      max_wait_seconds: float = 3600.0) -> dict:
        """Poll until terminal state or timeout. Raises JiraTaskTimeoutError."""
        terminal = {"COMPLETE", "FAILED", "CANCEL_REQUESTED", "CANCELLED", "DEAD"}
        deadline = time.monotonic() + max_wait_seconds

        while True:
            status = self.get_task_status(task_id)
            if status.get("status") in terminal:
                return status
            if time.monotonic() >= deadline:
                raise JiraTaskTimeoutError(
                    f"Task {task_id} did not finish within {max_wait_seconds}s."
                )
            time.sleep(poll_interval)

    def delete_issue(self, issue_id_or_key: str, delete_subtasks: bool = True) -> None:
        """Permanently delete a Jira issue (used by DELETE_TEST / DELETE_TEST_EXECUTION in V2)."""
        self._request(
            "DELETE",
            f"/rest/api/3/issue/{issue_id_or_key}",
            params={"deleteSubtasks": "true" if delete_subtasks else "false"},
        )


# =============================================================================
# Configuration
# =============================================================================

XIA_PROJECT_KEY = "XIA"
XIA_ISSUE_TYPE_TEST_ID = "10005"            # V1: ARCHIVE_TEST re-validation
XIA_ISSUE_TYPE_TEST_EXECUTION_ID = "10008"  # V2: DELETE_TEST_EXECUTION re-validation
XIA_NIGHTLY_USER = "xia_nightly"

JQL_CHUNK_SIZE = 100

SKIP_ISSUE_NOT_FOUND = "issue_not_found"
SKIP_WRONG_ISSUE_TYPE = "wrong_issue_type"
SKIP_WRONG_SPACE = "wrong_space"
SKIP_ALREADY_IN_XIA = "already_in_xia"


# =============================================================================
# CSV helpers
# Keboola exposes input tables as CSV files under in/tables/ relative to /data/.
# The transformation's working directory is /data/, so paths are relative.
# =============================================================================

IN_DIR = "in/tables"
OUT_DIR = "out/tables"


def read_csv(table_name: str) -> tuple:
    """Read a Keboola input table. Returns (rows: list[dict], fieldnames: list[str])."""
    path = f"{IN_DIR}/{table_name}.csv"
    with open(path, mode="rt", encoding="utf-8") as f:
        lazy_lines = (line.replace("\0", "") for line in f)
        reader = csv.DictReader(lazy_lines)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv(table_name: str, rows: list, fieldnames: list) -> None:
    """Write a Keboola output table (full replace)."""
    os.makedirs(OUT_DIR, exist_ok=True)
    path = f"{OUT_DIR}/{table_name}"
    with open(path, mode="wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, extrasaction="ignore", restval=""
        )
        writer.writeheader()
        writer.writerows(rows)


# =============================================================================
# Helpers
# =============================================================================

def now_cz() -> str:
    """Current Prague time as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.datetime.now(pytz.timezone("Europe/Prague")).strftime("%Y-%m-%d %H:%M:%S")


def chunks(lst: list, size: int):
    """Yield successive slices of a list."""
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def run_sequence_for(request_key: str, runs_rows: list) -> int:
    """Next 1-based RUN_SEQUENCE for a given XIA_REQUEST_KEY."""
    existing = [r for r in runs_rows if str(r["XIA_REQUEST_KEY"]) == str(request_key)]
    return len(existing) + 1


def insert_run(runs_rows: list, request_key: str, run_sequence: int,
               started: str, finished: str,
               total: int, success: int, failed: int, skipped: int,
               error_summary: str = "") -> None:
    """Append a new XIA_RUNS row."""
    if success == 0:
        run_status = "FAILED"
    elif success == total:
        run_status = "SUCCESS"
    else:
        run_status = "PARTIAL_SUCCESS"

    timestamp = now_cz()
    runs_rows.append({
        "XIA_RUN_KEY": str(uuid.uuid4()),
        "XIA_REQUEST_KEY": request_key,
        "RUN_SEQUENCE": run_sequence,
        "RUN_TYPE": "EXECUTE",
        "RUN_STATUS": run_status,
        "STARTED_DATETIME": started,
        "FINISHED_DATETIME": finished,
        "ITEMS_TOTAL_COUNT": total,
        "ITEMS_SUCCESS_COUNT": success,
        "ITEMS_FAILED_COUNT": failed,
        "ITEMS_SKIPPED_COUNT": skipped,
        "ERROR_SUMMARY": error_summary,
        "INSERTED_BY": XIA_NIGHTLY_USER,
        "INSERTED_DATETIME": timestamp,
        "UPDATED_BY": "",
        "UPDATED_DATETIME": "",
    })


def _skip_item(item: dict, reason: str, timestamp: str) -> None:
    item["ITEM_STATUS"] = "SKIPPED"
    item["ERROR_CODE"] = reason
    item["ERROR_MESSAGE"] = f"SKIPPED: {reason}"
    item["EXECUTED_DATETIME"] = timestamp
    item["UPDATED_BY"] = XIA_NIGHTLY_USER
    item["UPDATED_DATETIME"] = timestamp


def _mark_all_failed(items: list, error_message: str, timestamp: str) -> None:
    for item in items:
        item["ITEM_STATUS"] = "FAILED"
        item["ERROR_CODE"] = "JIRA_ERROR"
        item["ERROR_MESSAGE"] = error_message
        item["EXECUTED_DATETIME"] = timestamp
        item["UPDATED_BY"] = XIA_NIGHTLY_USER
        item["UPDATED_DATETIME"] = timestamp


def _request_status(moved: int, total_pending: int) -> str:
    if moved == total_pending:
        return "SUCCESS"
    if moved > 0:
        return "PARTIAL_SUCCESS"
    return "FAILED"


# =============================================================================
# Core processing
# =============================================================================

def process_request(jira: JiraApiClient, request: dict,
                    all_items: list, all_runs: list) -> None:
    """Dispatch a PENDING request to the correct operation handler."""
    operation_type = request.get("OPERATION_TYPE", "ARCHIVE_TEST")
    if operation_type == "ARCHIVE_TEST":
        _run_archive_test(jira, request, all_items, all_runs)
    elif operation_type in ("DELETE_TEST", "DELETE_TEST_EXECUTION"):
        # V2: not yet implemented — fail fast so other requests still run
        request_ref = request.get("REQUEST_REF", request.get("KEY"))
        error_msg = f"{operation_type} not implemented — deferred to V2."
        print(f"[{request_ref}] {error_msg}")
        request["STATUS"] = "FAILED"
        request["UPDATED_BY"] = XIA_NIGHTLY_USER
        request["UPDATED_DATETIME"] = now_cz()
        insert_run(all_runs, request["KEY"],
                   run_sequence_for(request["KEY"], all_runs),
                   now_cz(), now_cz(), 0, 0, 0, 0, error_msg)
    else:
        request_ref = request.get("REQUEST_REF", request.get("KEY"))
        error_msg = f"Unknown OPERATION_TYPE: {operation_type}"
        print(f"[{request_ref}] {error_msg}")
        request["STATUS"] = "FAILED"
        request["UPDATED_BY"] = XIA_NIGHTLY_USER
        request["UPDATED_DATETIME"] = now_cz()
        insert_run(all_runs, request["KEY"],
                   run_sequence_for(request["KEY"], all_runs),
                   now_cz(), now_cz(), 0, 0, 0, 0, error_msg)


def _run_archive_test(jira: JiraApiClient, request: dict,
                      all_items: list, all_runs: list) -> None:
    """ARCHIVE_TEST: bulk-move PENDING items to XIA space (V1 operation)."""
    req_key = request["KEY"]
    request_ref = request.get("REQUEST_REF", req_key)
    started = now_cz()
    print(f"[{request_ref}] Processing...")

    # Collect PENDING items for this request
    req_items = [
        item for item in all_items
        if str(item["XIA_REQUEST_KEY"]) == str(req_key)
        and item["ITEM_STATUS"] == "PENDING"
    ]

    if not req_items:
        print(f"[{request_ref}] No PENDING items — marking FAILED.")
        request["STATUS"] = "FAILED"
        request["UPDATED_BY"] = XIA_NIGHTLY_USER
        request["UPDATED_DATETIME"] = now_cz()
        insert_run(all_runs, req_key, run_sequence_for(req_key, all_runs),
                   started, now_cz(), 0, 0, 0, 0, "No PENDING items found.")
        return

    total_count = len(req_items)
    run_seq = run_sequence_for(req_key, all_runs)

    # Lock: PENDING → RUNNING
    request["STATUS"] = "RUNNING"
    request["UPDATED_BY"] = XIA_NIGHTLY_USER
    request["UPDATED_DATETIME"] = started

    # --- Re-validation via Jira JQL ---
    id_to_item = {item["ISSUE_ID"]: item for item in req_items}
    found_ids = set()

    for chunk in chunks(list(id_to_item.keys()), JQL_CHUNK_SIZE):
        jql = "id in (" + ",".join(chunk) + ")"
        try:
            issues = jira.search_issues_by_jql(jql, fields=["issuetype", "project"])
        except Exception as e:
            error_msg = f"Re-validation JQL failed: {e}"
            print(f"[{request_ref}] {error_msg}")
            _mark_all_failed(req_items, error_msg, now_cz())
            request["STATUS"] = "FAILED"
            request["UPDATED_BY"] = XIA_NIGHTLY_USER
            request["UPDATED_DATETIME"] = now_cz()
            insert_run(all_runs, req_key, run_seq, started, now_cz(),
                       total_count, 0, total_count, 0, error_msg)
            return

        for issue in issues:
            issue_id = str(issue["id"])
            found_ids.add(issue_id)
            if issue_id not in id_to_item:
                continue
            item = id_to_item[issue_id]
            project_key = issue["fields"]["project"]["key"]
            issue_type_id = issue["fields"]["issuetype"]["id"]

            timestamp = now_cz()
            if project_key == XIA_PROJECT_KEY:
                _skip_item(item, SKIP_ALREADY_IN_XIA, timestamp)
            elif project_key != item.get("SOURCE_SPACE_KEY", ""):
                _skip_item(item, SKIP_WRONG_SPACE, timestamp)
            elif issue_type_id != XIA_ISSUE_TYPE_TEST_ID:
                _skip_item(item, SKIP_WRONG_ISSUE_TYPE, timestamp)
            # else: ITEM_STATUS remains PENDING — eligible for move

    # Items not returned by Jira at all
    for issue_id, item in id_to_item.items():
        if issue_id not in found_ids and item["ITEM_STATUS"] == "PENDING":
            _skip_item(item, SKIP_ISSUE_NOT_FOUND, now_cz())

    eligible_items = [i for i in req_items if i["ITEM_STATUS"] == "PENDING"]
    skipped_count = sum(1 for i in req_items if i["ITEM_STATUS"] == "SKIPPED")

    if not eligible_items:
        print(f"[{request_ref}] All {skipped_count} items skipped — marking FAILED.")
        request["STATUS"] = "FAILED"
        request["UPDATED_BY"] = XIA_NIGHTLY_USER
        request["UPDATED_DATETIME"] = now_cz()
        insert_run(all_runs, req_key, run_seq, started, now_cz(),
                   total_count, 0, 0, skipped_count,
                   "All items skipped during re-validation.")
        return

    eligible_ids = [item["ISSUE_ID"] for item in eligible_items]
    print(f"[{request_ref}] {len(eligible_ids)} eligible, {skipped_count} skipped. Submitting bulk move...")

    # --- Bulk move ---
    try:
        task_id = jira.bulk_move_issues(eligible_ids, XIA_PROJECT_KEY, XIA_ISSUE_TYPE_TEST_ID)
    except JiraApiError as e:
        error_msg = str(e)
        print(f"[{request_ref}] bulk_move_issues failed: {error_msg}")
        _mark_all_failed(eligible_items, f"bulk_move failed: {error_msg}", now_cz())
        request["STATUS"] = "FAILED"
        request["UPDATED_BY"] = XIA_NIGHTLY_USER
        request["UPDATED_DATETIME"] = now_cz()
        insert_run(all_runs, req_key, run_seq, started, now_cz(),
                   total_count, 0, len(eligible_items), skipped_count, error_msg)
        return

    print(f"[{request_ref}] Task {task_id} submitted. Polling...")

    # --- Poll task ---
    try:
        task_result = jira.wait_for_task(task_id, poll_interval=5.0, max_wait_seconds=3600.0)
    except Exception as e:
        error_msg = str(e)
        print(f"[{request_ref}] Task polling failed: {error_msg}")
        _mark_all_failed(eligible_items, error_msg, now_cz())
        request["STATUS"] = "FAILED"
        request["UPDATED_BY"] = XIA_NIGHTLY_USER
        request["UPDATED_DATETIME"] = now_cz()
        insert_run(all_runs, req_key, run_seq, started, now_cz(),
                   total_count, 0, len(eligible_items), skipped_count, error_msg)
        return

    finished = now_cz()

    # --- Apply outcome ---
    if task_result.get("status") == "COMPLETE":
        for item in eligible_items:
            item["ITEM_STATUS"] = "MOVED"
            item["EXECUTED_DATETIME"] = finished
            item["UPDATED_BY"] = XIA_NIGHTLY_USER
            item["UPDATED_DATETIME"] = finished
        moved_count = len(eligible_items)
        failed_move_count = 0
        print(f"[{request_ref}] Task COMPLETE — {moved_count} items moved.")
    else:
        task_status = task_result.get("status", "UNKNOWN")
        task_msg = (task_result.get("message")
                    or task_result.get("progressMessage")
                    or task_status)
        error_msg = f"Task {task_status}: {task_msg}"
        _mark_all_failed(eligible_items, error_msg, finished)
        moved_count = 0
        failed_move_count = len(eligible_items)
        print(f"[{request_ref}] Task {task_status} — {failed_move_count} items failed.")

    final_status = _request_status(moved_count, total_count)
    request["STATUS"] = final_status
    request["UPDATED_BY"] = XIA_NIGHTLY_USER
    request["UPDATED_DATETIME"] = finished

    error_summary = (
        "" if final_status == "SUCCESS"
        else f"{failed_move_count} failed, {skipped_count} skipped"
    )
    insert_run(all_runs, req_key, run_seq, started, finished,
               total_count, moved_count, failed_move_count, skipped_count,
               error_summary)

    print(f"[{request_ref}] Done — {final_status}.")


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    print("XIA Nightly — starting.")

    # TCP connectivity test
    host = JIRA_BASE_URL.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, 443))
        sock.close()
        if result == 0:
            print(f"[TCP test] {host}:443 — OK")
        else:
            print(f"[TCP test] {host}:443 — FAILED (errno {result})")
    except Exception as e:
        print(f"[TCP test] {host}:443 — EXCEPTION: {e}")

    # Load input tables (Keboola provides them as CSV under in/tables/)
    requests_rows, requests_fields = read_csv("XIA_REQUESTS_DEV")
    items_rows, items_fields = read_csv("XIA_REQUEST_ITEMS_DEV")
    runs_rows, runs_fields = read_csv("XIA_RUNS_DEV")

    # Crash recovery: reset any requests stuck in RUNNING (from a previous crashed run)
    recovered = 0
    for row in requests_rows:
        if row.get("STATUS") == "RUNNING":
            row["STATUS"] = "PENDING"
            row["UPDATED_BY"] = XIA_NIGHTLY_USER
            row["UPDATED_DATETIME"] = now_cz()
            recovered += 1
    if recovered:
        print(f"Crash recovery: reset {recovered} RUNNING request(s) to PENDING.")

    pending = [r for r in requests_rows if r.get("STATUS") == "PENDING"]
    print(f"Found {len(pending)} PENDING request(s).")

    if not pending:
        print("Nothing to do.")
        write_csv("XIA_REQUESTS_DEV", requests_rows, requests_fields)
        write_csv("XIA_REQUEST_ITEMS_DEV", items_rows, items_fields)
        write_csv("XIA_RUNS_DEV", runs_rows, runs_fields)
        return

    jira = JiraApiClient(JIRA_BASE_URL, JIRA_USERNAME, JIRA_API_TOKEN)

    for request in pending:
        try:
            process_request(jira, request, items_rows, runs_rows)
        except Exception as e:
            # Safeguard: an unhandled exception in one request must not abort others.
            request_ref = request.get("REQUEST_REF", request.get("KEY", "?"))
            print(f"[{request_ref}] UNHANDLED ERROR: {e}")
            request["STATUS"] = "FAILED"
            request["UPDATED_BY"] = XIA_NIGHTLY_USER
            request["UPDATED_DATETIME"] = now_cz()

    # Write output tables — Keboola Replace mode overwrites Storage
    write_csv("XIA_REQUESTS_DEV", requests_rows, requests_fields)
    write_csv("XIA_REQUEST_ITEMS_DEV", items_rows, items_fields)
    write_csv("XIA_RUNS_DEV", runs_rows, runs_fields)

    print("XIA Nightly — finished.")


# Keboola convention: call at module level, no if __name__ == "__main__" guard
main()
