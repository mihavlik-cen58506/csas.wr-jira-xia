"""Minimal Jira REST API v3 client for the XIA nightly job.

Ported from the reference transformation (script.py / context/XIA/xia_nightly_main.py)
to preserve identical request/response handling and error parsing.
"""

import base64
import logging
import time

import requests
from requests.exceptions import Timeout

from constants import (
    JIRA_REQUEST_TIMEOUT_SECONDS,
    JIRA_TASK_MAX_WAIT_SECONDS,
    JIRA_TASK_POLL_INTERVAL_SECONDS,
    JIRA_TASK_TERMINAL_STATUSES,
)


class JiraApiError(Exception):
    """Raised when a Jira API request fails."""

    def __init__(
        self, summary, *, status_code=None, path=None, method=None, messages=None, field_errors=None, raw=None
    ):
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
    """Extract a readable message from a Jira error response body."""
    messages = []
    field_errors = {}

    if not isinstance(body, dict):
        if body:
            messages.append(str(body))
        return messages, field_errors

    error_messages = body.get("errorMessages")
    if isinstance(error_messages, list):
        messages.extend(str(m) for m in error_messages if m)

    errors = body.get("errors")
    if isinstance(errors, dict):
        field_errors.update({str(k): str(v) for k, v in errors.items()})
    elif isinstance(errors, list):
        for item in errors:
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
                timeout=JIRA_REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except Timeout:
            raise JiraApiError(
                f"Request to {path} timed out after {JIRA_REQUEST_TIMEOUT_SECONDS} seconds.",
                path=path,
                method=method,
                messages=[f"Network timeout after {JIRA_REQUEST_TIMEOUT_SECONDS} seconds."],
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
                status_code=response.status_code,
                path=path,
                method=method,
                messages=messages,
                field_errors=field_errors,
                raw=body,
            )

        return response

    def search_issues_by_jql(
        self, jql: str, fields: list | None = None, max_pages: int = 50, page_size: int = 100
    ) -> list:
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

    def bulk_move_issues(
        self,
        issue_ids_or_keys: list,
        target_project_key: str,
        target_issue_type_id: str,
        send_bulk_notification: bool = True,
    ) -> str:
        """Submit async bulk-move request. Returns the taskId to poll."""
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
        logging.info("Bulk move submitted, taskId=%s", task_id)
        return task_id

    def get_task_status(self, task_id: str) -> dict:
        return self._request("GET", f"/rest/api/3/task/{task_id}").json()

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: float = JIRA_TASK_POLL_INTERVAL_SECONDS,
        max_wait_seconds: float = JIRA_TASK_MAX_WAIT_SECONDS,
    ) -> dict:
        """Poll until terminal state or timeout. Raises JiraTaskTimeoutError."""
        deadline = time.monotonic() + max_wait_seconds

        while True:
            status = self.get_task_status(task_id)
            if status.get("status") in JIRA_TASK_TERMINAL_STATUSES:
                return status
            if time.monotonic() >= deadline:
                raise JiraTaskTimeoutError(f"Task {task_id} did not finish within {max_wait_seconds}s.")
            time.sleep(poll_interval)
