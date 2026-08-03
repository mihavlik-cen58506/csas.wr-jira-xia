import unittest
from unittest import mock

from freezegun import freeze_time

from jira_api import JiraApiError, JiraTaskTimeoutError
from processor import (
    derive_request_status,
    derive_run_status,
    next_run_sequence_for,
    process_pending_requests,
    process_request,
    recover_stuck_requests,
)


def _issue(issue_id, project_key, issue_type_id):
    return {
        "id": str(issue_id),
        "fields": {
            "project": {"key": project_key},
            "issuetype": {"id": issue_type_id},
        },
    }


def _request(key="req-1", operation_type="ARCHIVE_TEST", status="PENDING"):
    return {
        "KEY": key,
        "REQUEST_REF": key,
        "OPERATION_TYPE": operation_type,
        "STATUS": status,
        "UPDATED_BY": "",
        "UPDATED_DATETIME": "",
    }


def _item(request_key, issue_id, source_space_key="ABC", status="PENDING"):
    return {
        "XIA_REQUEST_KEY": request_key,
        "ISSUE_ID": str(issue_id),
        "SOURCE_SPACE_KEY": source_space_key,
        "ITEM_STATUS": status,
        "ERROR_CODE": "",
        "ERROR_MESSAGE": "",
        "EXECUTED_DATETIME": "",
        "UPDATED_BY": "",
        "UPDATED_DATETIME": "",
    }


class TestRunStatusDerivation(unittest.TestCase):
    def test_success_when_all_moved(self):
        self.assertEqual(derive_run_status(3, 3), "SUCCESS")

    def test_partial_when_some_moved(self):
        self.assertEqual(derive_run_status(5, 3), "PARTIAL_SUCCESS")

    def test_failed_when_none_moved(self):
        self.assertEqual(derive_run_status(3, 0), "FAILED")


class TestRequestStatusDerivation(unittest.TestCase):
    def test_success_when_moved_equals_total(self):
        self.assertEqual(derive_request_status(3, 3), "SUCCESS")

    def test_partial_success_when_some_moved(self):
        self.assertEqual(derive_request_status(3, 5), "PARTIAL_SUCCESS")

    def test_failed_when_none_moved(self):
        self.assertEqual(derive_request_status(0, 5), "FAILED")


class TestCrashRecovery(unittest.TestCase):
    @freeze_time("2026-08-03 10:00:00")
    def test_running_requests_reset_to_pending(self):
        requests_rows = [_request(status="RUNNING"), _request(key="req-2", status="PENDING")]
        recovered = recover_stuck_requests(requests_rows)

        self.assertEqual(recovered, 1)
        self.assertEqual(requests_rows[0]["STATUS"], "PENDING")
        self.assertEqual(requests_rows[0]["UPDATED_BY"], "xia_nightly")
        self.assertEqual(requests_rows[1]["STATUS"], "PENDING")


class TestRunSequenceFor(unittest.TestCase):
    def test_increments_per_request(self):
        runs = [{"XIA_REQUEST_KEY": "req-1"}, {"XIA_REQUEST_KEY": "req-1"}, {"XIA_REQUEST_KEY": "req-2"}]
        self.assertEqual(next_run_sequence_for("req-1", runs), 3)
        self.assertEqual(next_run_sequence_for("req-2", runs), 2)
        self.assertEqual(next_run_sequence_for("req-3", runs), 1)


class TestArchiveTestFlow(unittest.TestCase):
    """AC2, AC3, AC4, AC5 from specs.md §12."""

    @freeze_time("2026-08-03 10:00:00")
    def test_happy_path_all_eligible_moved(self):
        # AC2: 3 valid eligible items, bulk move COMPLETE -> all MOVED, request SUCCESS
        request = _request()
        items = [_item("req-1", 1), _item("req-1", 2), _item("req-1", 3)]
        runs = []

        jira = mock.Mock()
        jira.search_issues_by_jql.return_value = [
            _issue(1, "ABC", "10005"),
            _issue(2, "ABC", "10005"),
            _issue(3, "ABC", "10005"),
        ]
        jira.bulk_move_issues.return_value = "task-1"
        jira.wait_for_task.return_value = {"status": "COMPLETE"}

        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "SUCCESS")
        self.assertTrue(all(i["ITEM_STATUS"] == "MOVED" for i in items))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["ITEMS_TOTAL_COUNT"], 3)
        self.assertEqual(runs[0]["ITEMS_SUCCESS_COUNT"], 3)
        self.assertEqual(runs[0]["ITEMS_FAILED_COUNT"], 0)
        self.assertEqual(runs[0]["ITEMS_SKIPPED_COUNT"], 0)
        self.assertEqual(runs[0]["RUN_STATUS"], "SUCCESS")

    @freeze_time("2026-08-03 10:00:00")
    def test_partial_success_via_skip(self):
        # AC3: 5 items, 2 fail revalidation (skip), 3 eligible -> PARTIAL_SUCCESS
        request = _request()
        items = [
            _item("req-1", 1),  # eligible
            _item("req-1", 2),  # eligible
            _item("req-1", 3),  # eligible
            _item("req-1", 4),  # already in XIA -> skipped
            _item("req-1", 5, source_space_key="XYZ"),  # wrong space -> skipped
        ]
        runs = []

        jira = mock.Mock()
        jira.search_issues_by_jql.return_value = [
            _issue(1, "ABC", "10005"),
            _issue(2, "ABC", "10005"),
            _issue(3, "ABC", "10005"),
            _issue(4, "XIA", "10005"),
            _issue(5, "ABC", "10005"),
        ]
        jira.bulk_move_issues.return_value = "task-1"
        jira.wait_for_task.return_value = {"status": "COMPLETE"}

        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "PARTIAL_SUCCESS")
        self.assertEqual(items[3]["ITEM_STATUS"], "SKIPPED")
        self.assertEqual(items[3]["ERROR_CODE"], "already_in_xia")
        self.assertEqual(items[4]["ITEM_STATUS"], "SKIPPED")
        self.assertEqual(items[4]["ERROR_CODE"], "wrong_space")
        self.assertEqual(sum(1 for i in items if i["ITEM_STATUS"] == "MOVED"), 3)
        self.assertEqual(runs[0]["ITEMS_TOTAL_COUNT"], 5)
        self.assertEqual(runs[0]["ITEMS_SUCCESS_COUNT"], 3)
        self.assertEqual(runs[0]["ITEMS_FAILED_COUNT"], 0)
        self.assertEqual(runs[0]["ITEMS_SKIPPED_COUNT"], 2)

    @freeze_time("2026-08-03 10:00:00")
    def test_revalidation_hard_failure(self):
        # AC4: Jira search call fails -> all pending items FAILED, request FAILED, one FAILED run
        request = _request()
        items = [_item("req-1", 1), _item("req-1", 2)]
        runs = []

        jira = mock.Mock()
        jira.search_issues_by_jql.side_effect = JiraApiError("boom")

        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "FAILED")
        self.assertTrue(all(i["ITEM_STATUS"] == "FAILED" for i in items))
        self.assertTrue(all(i["ERROR_CODE"] == "JIRA_ERROR" for i in items))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["RUN_STATUS"], "FAILED")

    @freeze_time("2026-08-03 10:00:00")
    def test_bulk_move_task_non_complete(self):
        # AC5: task ends non-COMPLETE -> eligible items FAILED, moved_count = 0
        request = _request()
        items = [_item("req-1", 1), _item("req-1", 2)]
        runs = []

        jira = mock.Mock()
        jira.search_issues_by_jql.return_value = [_issue(1, "ABC", "10005"), _issue(2, "ABC", "10005")]
        jira.bulk_move_issues.return_value = "task-1"
        jira.wait_for_task.return_value = {"status": "FAILED", "message": "boom"}

        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "FAILED")
        self.assertTrue(all(i["ITEM_STATUS"] == "FAILED" for i in items))
        self.assertEqual(runs[0]["ITEMS_SUCCESS_COUNT"], 0)
        self.assertEqual(runs[0]["ITEMS_FAILED_COUNT"], 2)

    @freeze_time("2026-08-03 10:00:00")
    def test_polling_timeout_marks_failed(self):
        request = _request()
        items = [_item("req-1", 1)]
        runs = []

        jira = mock.Mock()
        jira.search_issues_by_jql.return_value = [_issue(1, "ABC", "10005")]
        jira.bulk_move_issues.return_value = "task-1"
        jira.wait_for_task.side_effect = JiraTaskTimeoutError("timed out")

        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "FAILED")
        self.assertEqual(items[0]["ITEM_STATUS"], "FAILED")
        self.assertEqual(runs[0]["RUN_STATUS"], "FAILED")

    @freeze_time("2026-08-03 10:00:00")
    def test_no_pending_items_marks_request_failed(self):
        request = _request()
        items = [_item("req-1", 1, status="MOVED")]
        runs = []

        jira = mock.Mock()
        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "FAILED")
        self.assertEqual(runs[0]["ERROR_SUMMARY"], "No PENDING items found.")
        jira.search_issues_by_jql.assert_not_called()

    @freeze_time("2026-08-03 10:00:00")
    def test_unsupported_operation_type_marks_failed(self):
        request = _request(operation_type="DELETE_TEST")
        items = []
        runs = []

        jira = mock.Mock()
        process_request(jira, request, items, runs)

        self.assertEqual(request["STATUS"], "FAILED")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["RUN_TYPE"], "EXECUTE")
        jira.search_issues_by_jql.assert_not_called()


class TestEmptyQueue(unittest.TestCase):
    @freeze_time("2026-08-03 10:00:00")
    def test_no_pending_requests_writes_no_new_runs(self):
        # AC1: no request with STATUS == PENDING -> only crash recovery, no new XIA_RUNS rows
        requests_rows = [_request(status="SUCCESS"), _request(key="req-2", status="FAILED")]
        items_rows = []
        runs_rows = []

        jira = mock.Mock()
        process_pending_requests(jira, requests_rows, items_rows, runs_rows)

        self.assertEqual(runs_rows, [])
        self.assertEqual(requests_rows[0]["STATUS"], "SUCCESS")
        self.assertEqual(requests_rows[1]["STATUS"], "FAILED")
        jira.search_issues_by_jql.assert_not_called()

    @freeze_time("2026-08-03 10:00:00")
    def test_stuck_running_request_recovered_then_reprocessed_same_run(self):
        # AC6: RUNNING request is reset to PENDING before request selection, so it is picked up
        # and reprocessed within the same run (here: FAILED, since it has no pending items left).
        requests_rows = [_request(status="RUNNING")]
        items_rows = []
        runs_rows = []

        jira = mock.Mock()
        process_pending_requests(jira, requests_rows, items_rows, runs_rows)

        self.assertEqual(requests_rows[0]["STATUS"], "FAILED")
        self.assertEqual(len(runs_rows), 1)
        self.assertEqual(runs_rows[0]["ERROR_SUMMARY"], "No PENDING items found.")


if __name__ == "__main__":
    unittest.main()
