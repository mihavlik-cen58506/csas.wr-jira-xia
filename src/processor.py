"""Business logic for the XIA nightly ARCHIVE_TEST flow: revalidate, bulk-move, record results."""

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from constants import (
    DATETIME_FORMAT,
    ERROR_CODE_JIRA_ERROR,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_MOVED,
    ITEM_STATUS_PENDING,
    ITEM_STATUS_SKIPPED,
    JQL_CHUNK_SIZE,
    OPERATION_TYPE_ARCHIVE_TEST,
    RUN_TYPE_EXECUTE,
    SKIP_ALREADY_IN_XIA,
    SKIP_ISSUE_NOT_FOUND,
    SKIP_WRONG_ISSUE_TYPE,
    SKIP_WRONG_SPACE,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    TIMEZONE,
    XIA_ISSUE_TYPE_TEST_ID,
    XIA_NIGHTLY_USER,
    XIA_PROJECT_KEY,
)
from jira_api import JiraApiClient, JiraApiError, JiraTaskTimeoutError


def now_prague() -> str:
    """Current time in Prague, formatted 'YYYY-MM-DD HH:MM:SS' for audit/timestamp columns."""
    return datetime.now(ZoneInfo(TIMEZONE)).strftime(DATETIME_FORMAT)


def chunks(items: list, size: int):
    """Yield successive slices of a list."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def next_run_sequence_for(request_key: str, runs_rows: list) -> int:
    """1-based sequence number for the next run row of this request (existing count + 1)."""
    existing = [r for r in runs_rows if str(r.get("XIA_REQUEST_KEY")) == str(request_key)]
    return len(existing) + 1


def derive_run_status(items_total_count: int, items_success_count: int) -> str:
    """FAILED if nothing succeeded, SUCCESS if everything did, otherwise PARTIAL_SUCCESS."""
    if items_success_count == 0:
        return STATUS_FAILED
    if items_success_count == items_total_count:
        return STATUS_SUCCESS
    return STATUS_PARTIAL_SUCCESS


def derive_request_status(moved_count: int, total_count: int) -> str:
    """Same SUCCESS/PARTIAL_SUCCESS/FAILED rule as derive_run_status, applied to the request."""
    if moved_count == total_count:
        return STATUS_SUCCESS
    if moved_count > 0:
        return STATUS_PARTIAL_SUCCESS
    return STATUS_FAILED


def insert_run(
    runs_rows: list,
    request_key: str,
    run_sequence: int,
    started: str,
    finished: str,
    total: int,
    success: int,
    failed: int,
    skipped: int,
    error_summary: str = "",
) -> None:
    """Append one XIA_RUNS row recording the outcome of processing a request."""
    run_status = derive_run_status(total, success)
    timestamp = now_prague()
    runs_rows.append(
        {
            "XIA_RUN_KEY": str(uuid.uuid4()),
            "XIA_REQUEST_KEY": request_key,
            "RUN_SEQUENCE": run_sequence,
            "RUN_TYPE": RUN_TYPE_EXECUTE,
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
        }
    )


def _skip_item(item: dict, reason: str, timestamp: str) -> None:
    item["ITEM_STATUS"] = ITEM_STATUS_SKIPPED
    item["ERROR_CODE"] = reason
    item["ERROR_MESSAGE"] = f"SKIPPED: {reason}"
    item["EXECUTED_DATETIME"] = timestamp
    item["UPDATED_BY"] = XIA_NIGHTLY_USER
    item["UPDATED_DATETIME"] = timestamp


def _mark_all_failed(items: list, error_message: str, timestamp: str) -> None:
    for item in items:
        item["ITEM_STATUS"] = ITEM_STATUS_FAILED
        item["ERROR_CODE"] = ERROR_CODE_JIRA_ERROR
        item["ERROR_MESSAGE"] = error_message
        item["EXECUTED_DATETIME"] = timestamp
        item["UPDATED_BY"] = XIA_NIGHTLY_USER
        item["UPDATED_DATETIME"] = timestamp


def _set_request_status(request: dict, status: str, timestamp: str) -> None:
    request["STATUS"] = status
    request["UPDATED_BY"] = XIA_NIGHTLY_USER
    request["UPDATED_DATETIME"] = timestamp


def _fail_request(
    request: dict,
    all_runs: list,
    request_ref: str,
    req_key: str,
    run_seq: int,
    started: str,
    total_count: int,
    skipped_count: int,
    items_to_fail: list,
    error_summary: str,
    log_message: str | None = None,
) -> None:
    """Mark request FAILED, fail `items_to_fail` (if any), and insert one FAILED run row.

    Shared by every hard-failure branch of the ARCHIVE_TEST flow so the
    log + mark-failed + set-status + insert-run sequence isn't repeated at each call site.
    """
    logging.info("[%s] %s", request_ref, log_message or error_summary)
    finished = now_prague()
    if items_to_fail:
        _mark_all_failed(items_to_fail, error_summary, finished)
    _set_request_status(request, STATUS_FAILED, finished)
    insert_run(
        all_runs, req_key, run_seq, started, finished, total_count, 0, len(items_to_fail), skipped_count, error_summary
    )


def recover_stuck_requests(requests_rows: list) -> int:
    """Reset requests stuck in RUNNING (from a crashed previous run) back to PENDING."""
    recovered = 0
    for row in requests_rows:
        if row.get("STATUS") == STATUS_RUNNING:
            _set_request_status(row, STATUS_PENDING, now_prague())
            recovered += 1
    return recovered


def select_pending_requests(requests_rows: list) -> list:
    """Requests eligible for processing this run."""
    return [r for r in requests_rows if r.get("STATUS") == STATUS_PENDING]


def process_request(jira: JiraApiClient, request: dict, all_items: list, all_runs: list) -> None:
    """Handle one PENDING request; only ARCHIVE_TEST is supported, anything else fails fast."""
    operation_type = request.get("OPERATION_TYPE")
    request_ref = request.get("REQUEST_REF", request.get("KEY"))

    if operation_type == OPERATION_TYPE_ARCHIVE_TEST:
        _process_archive_test(jira, request, all_items, all_runs)
        return

    error_msg = f"Unsupported OPERATION_TYPE: {operation_type}"
    logging.info("[%s] %s", request_ref, error_msg)
    timestamp = now_prague()
    _set_request_status(request, STATUS_FAILED, timestamp)
    insert_run(
        all_runs,
        request["KEY"],
        next_run_sequence_for(request["KEY"], all_runs),
        timestamp,
        timestamp,
        0,
        0,
        0,
        0,
        error_msg,
    )


def _process_archive_test(jira: JiraApiClient, request: dict, all_items: list, all_runs: list) -> None:
    """Revalidate this request's PENDING items in Jira, then bulk-move the eligible ones to XIA."""
    req_key = request["KEY"]
    request_ref = request.get("REQUEST_REF", req_key)
    started = now_prague()
    logging.info("[%s] Processing request.", request_ref)

    req_items = [
        item
        for item in all_items
        if str(item.get("XIA_REQUEST_KEY")) == str(req_key) and item.get("ITEM_STATUS") == ITEM_STATUS_PENDING
    ]

    if not req_items:
        _fail_request(
            request,
            all_runs,
            request_ref,
            req_key,
            next_run_sequence_for(req_key, all_runs),
            started,
            0,
            0,
            [],
            "No PENDING items found.",
            log_message="No PENDING items - marking FAILED.",
        )
        return

    total_count = len(req_items)
    run_seq = next_run_sequence_for(req_key, all_runs)

    # Lock: PENDING -> RUNNING
    _set_request_status(request, STATUS_RUNNING, started)

    # --- Revalidation via Jira JQL, in chunks of JQL_CHUNK_SIZE ---
    id_to_item = {item["ISSUE_ID"]: item for item in req_items}
    found_ids = set()

    for chunk in chunks(list(id_to_item.keys()), JQL_CHUNK_SIZE):
        jql = "id in (" + ",".join(chunk) + ")"
        try:
            issues = jira.search_issues_by_jql(jql, fields=["issuetype", "project"])
        except Exception as exc:
            error_msg = f"Re-validation JQL failed: {exc}"
            _fail_request(
                request, all_runs, request_ref, req_key, run_seq, started, total_count, 0, req_items, error_msg
            )
            return

        for issue in issues:
            issue_id = str(issue["id"])
            found_ids.add(issue_id)
            item = id_to_item.get(issue_id)
            if item is None:
                continue

            project_key = issue["fields"]["project"]["key"]
            issue_type_id = issue["fields"]["issuetype"]["id"]
            timestamp = now_prague()

            if project_key == XIA_PROJECT_KEY:
                _skip_item(item, SKIP_ALREADY_IN_XIA, timestamp)
            elif project_key != item.get("SOURCE_SPACE_KEY", ""):
                _skip_item(item, SKIP_WRONG_SPACE, timestamp)
            elif issue_type_id != XIA_ISSUE_TYPE_TEST_ID:
                _skip_item(item, SKIP_WRONG_ISSUE_TYPE, timestamp)
            # else: ITEM_STATUS remains PENDING - eligible for move

    # Any requested ISSUE_ID not returned by Jira at all
    for issue_id, item in id_to_item.items():
        if issue_id not in found_ids and item.get("ITEM_STATUS") == ITEM_STATUS_PENDING:
            _skip_item(item, SKIP_ISSUE_NOT_FOUND, now_prague())

    eligible_items = [i for i in req_items if i.get("ITEM_STATUS") == ITEM_STATUS_PENDING]
    skipped_count = sum(1 for i in req_items if i.get("ITEM_STATUS") == ITEM_STATUS_SKIPPED)

    if not eligible_items:
        _fail_request(
            request,
            all_runs,
            request_ref,
            req_key,
            run_seq,
            started,
            total_count,
            skipped_count,
            [],
            "All items skipped during re-validation.",
            log_message=f"All {skipped_count} items skipped - marking FAILED.",
        )
        return

    eligible_ids = [item["ISSUE_ID"] for item in eligible_items]
    logging.info("[%s] %d eligible, %d skipped. Submitting bulk move.", request_ref, len(eligible_ids), skipped_count)

    # --- Bulk move submission ---
    try:
        task_id = jira.bulk_move_issues(eligible_ids, XIA_PROJECT_KEY, XIA_ISSUE_TYPE_TEST_ID)
    except JiraApiError as exc:
        error_msg = f"bulk_move failed: {exc}"
        _fail_request(
            request,
            all_runs,
            request_ref,
            req_key,
            run_seq,
            started,
            total_count,
            skipped_count,
            eligible_items,
            error_msg,
        )
        return

    logging.info("[%s] Task %s submitted, polling.", request_ref, task_id)

    # --- Poll async task ---
    try:
        task_result = jira.wait_for_task(task_id)
    except (JiraApiError, JiraTaskTimeoutError) as exc:
        error_msg = str(exc)
        _fail_request(
            request,
            all_runs,
            request_ref,
            req_key,
            run_seq,
            started,
            total_count,
            skipped_count,
            eligible_items,
            error_msg,
            log_message=f"Task polling failed: {error_msg}",
        )
        return

    finished = now_prague()

    # --- Apply bulk-move outcome ---
    if task_result.get("status") == "COMPLETE":
        for item in eligible_items:
            item["ITEM_STATUS"] = ITEM_STATUS_MOVED
            item["EXECUTED_DATETIME"] = finished
            item["UPDATED_BY"] = XIA_NIGHTLY_USER
            item["UPDATED_DATETIME"] = finished
        moved_count = len(eligible_items)
        failed_move_count = 0
        logging.info("[%s] Task COMPLETE - %d items moved.", request_ref, moved_count)
    else:
        task_status = task_result.get("status", "UNKNOWN")
        task_msg = task_result.get("message") or task_result.get("progressMessage") or task_status
        error_msg = f"Task {task_status}: {task_msg}"
        _mark_all_failed(eligible_items, error_msg, finished)
        moved_count = 0
        failed_move_count = len(eligible_items)
        logging.info("[%s] Task %s - %d items failed.", request_ref, task_status, failed_move_count)

    final_status = derive_request_status(moved_count, total_count)
    _set_request_status(request, final_status, finished)

    error_summary = "" if final_status == STATUS_SUCCESS else f"{failed_move_count} failed, {skipped_count} skipped"
    insert_run(
        all_runs,
        req_key,
        run_seq,
        started,
        finished,
        total_count,
        moved_count,
        failed_move_count,
        skipped_count,
        error_summary,
    )

    logging.info("[%s] Done - %s.", request_ref, final_status)


def process_pending_requests(jira: JiraApiClient, requests_rows: list, items_rows: list, runs_rows: list) -> None:
    """Recover stuck requests, then process each PENDING one; one failing request must not abort the rest."""
    recovered = recover_stuck_requests(requests_rows)
    if recovered:
        logging.info("Crash recovery: reset %d RUNNING request(s) to PENDING.", recovered)

    pending = select_pending_requests(requests_rows)
    logging.info("Found %d PENDING request(s).", len(pending))

    for request in pending:
        try:
            process_request(jira, request, items_rows, runs_rows)
        except Exception as exc:  # noqa: BLE001 - must not abort the whole job
            request_ref = request.get("REQUEST_REF", request.get("KEY", "?"))
            logging.info("[%s] UNHANDLED ERROR: %s", request_ref, exc)
            _set_request_status(request, STATUS_FAILED, now_prague())
