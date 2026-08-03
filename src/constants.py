"""Fixed constants for the XIA nightly processor: statuses, skip reasons, Jira/timing settings."""

# Jira target for ARCHIVE_TEST moves
XIA_PROJECT_KEY = "XIA"
XIA_ISSUE_TYPE_TEST_ID = "10005"
XIA_NIGHTLY_USER = "xia_nightly"

# Revalidation batching
JQL_CHUNK_SIZE = 100

# Skip reasons
SKIP_ISSUE_NOT_FOUND = "issue_not_found"
SKIP_WRONG_ISSUE_TYPE = "wrong_issue_type"
SKIP_WRONG_SPACE = "wrong_space"
SKIP_ALREADY_IN_XIA = "already_in_xia"

# Request statuses
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
STATUS_FAILED = "FAILED"

# Request item statuses
ITEM_STATUS_PENDING = "PENDING"
ITEM_STATUS_SKIPPED = "SKIPPED"
ITEM_STATUS_MOVED = "MOVED"
ITEM_STATUS_FAILED = "FAILED"

ERROR_CODE_JIRA_ERROR = "JIRA_ERROR"

OPERATION_TYPE_ARCHIVE_TEST = "ARCHIVE_TEST"

RUN_TYPE_EXECUTE = "EXECUTE"

# Timezone
TIMEZONE = "Europe/Prague"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Jira API contract
JIRA_REQUEST_TIMEOUT_SECONDS = 30
JIRA_TASK_POLL_INTERVAL_SECONDS = 5.0
JIRA_TASK_MAX_WAIT_SECONDS = 3600.0
JIRA_TASK_TERMINAL_STATUSES = {"COMPLETE", "FAILED", "CANCEL_REQUESTED", "CANCELLED", "DEAD"}
JIRA_TASK_STATUS_COMPLETE = "COMPLETE"

# Input/output table role name-match patterns, case-insensitive "contains"
TABLE_ROLE_REQUESTS = "REQUESTS"
TABLE_ROLE_REQUEST_ITEMS = "REQUEST_ITEMS"
TABLE_ROLE_RUNS = "RUNS"

TABLE_ROLE_PATTERNS = {
    TABLE_ROLE_REQUEST_ITEMS: "XIA_REQUEST_ITEMS",
    TABLE_ROLE_REQUESTS: "XIA_REQUESTS",
    TABLE_ROLE_RUNS: "XIA_RUNS",
}

REQUIRED_COLUMNS = {
    TABLE_ROLE_REQUESTS: [
        "KEY",
        "REQUEST_REF",
        "OPERATION_TYPE",
        "STATUS",
        "UPDATED_BY",
        "UPDATED_DATETIME",
    ],
    TABLE_ROLE_REQUEST_ITEMS: [
        "XIA_REQUEST_KEY",
        "ISSUE_ID",
        "SOURCE_SPACE_KEY",
        "ITEM_STATUS",
        "ERROR_CODE",
        "ERROR_MESSAGE",
        "EXECUTED_DATETIME",
        "UPDATED_BY",
        "UPDATED_DATETIME",
    ],
    TABLE_ROLE_RUNS: [
        "XIA_RUN_KEY",
        "XIA_REQUEST_KEY",
        "RUN_SEQUENCE",
        "RUN_TYPE",
        "RUN_STATUS",
        "STARTED_DATETIME",
        "FINISHED_DATETIME",
        "ITEMS_TOTAL_COUNT",
        "ITEMS_SUCCESS_COUNT",
        "ITEMS_FAILED_COUNT",
        "ITEMS_SKIPPED_COUNT",
        "ERROR_SUMMARY",
        "INSERTED_BY",
        "INSERTED_DATETIME",
        "UPDATED_BY",
        "UPDATED_DATETIME",
    ],
}
