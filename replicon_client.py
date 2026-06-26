"""
replicon_client.py — Thin wrapper around the Deltek Replicon REST/JSON API.

API style notes:
- Every endpoint is POST {base_url}/services/{Service}.svc/{Operation}
- Request/response bodies are JSON, despite being WCF/SOAP-derived services
- Responses are wrapped in a top-level "d" key
- Tenant-scoped resources (projects, tasks, timesheets, users) are identified
  by URN strings, e.g. urn:replicon-tenant:{tenant_id}:project:{id}
"""

import uuid
import requests
from config import get_base_url, get_auth_headers, TULEAP_FIELD_URI


class RepliconAPIError(Exception):
    """Raised when the Replicon API returns an error payload."""
    pass


class TimesheetStateError(Exception):
    """Raised when a write operation is blocked due to the timesheet's current status."""
    pass


# Status URIs — both confirmed against real API responses.
TIMESHEET_STATUS_OPEN = "urn:replicon:timesheet-status:open"
TIMESHEET_STATUS_WAITING = "urn:replicon:timesheet-status:waiting"  # submitted, awaiting approval


class RepliconClient:
    def __init__(self):
        self.base_url = get_base_url()
        self.headers = {
            "Content-Type": "application/json",
            **get_auth_headers(),
        }

    def _post(self, service: str, operation: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}/services/{service}.svc/{operation}"
        response = requests.post(url, json=payload or {}, headers=self.headers, timeout=30)

        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise RepliconAPIError(f"HTTP error calling {service}/{operation}: {e}") from e

        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            raise RepliconAPIError(f"Replicon API error in {service}/{operation}: {body['error']}")

        # Most operations wrap the real payload in "d"; some return raw lists/objects
        return body.get("d", body) if isinstance(body, dict) else body

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    PROJECT_COLUMNS = [
        "urn:replicon:project-list-column:project",  # object-type column, returns real project uri
    ]

    def get_projects(self, page: int = 1, page_size: int = 50, text_search: str | None = None) -> dict:
        """
        List projects visible to the authenticated user.
        Confirmed filter shape (verified against live API):
          leftExpression.filterDefinitionUri = urn:replicon:project-list-filter:text
          operatorUri                        = urn:replicon:filter-operator:text-search
          rightExpression.value               = {"text": "..."}
        """
        filter_expression = None
        if text_search:
            filter_expression = {
                "leftExpression": {"filterDefinitionUri": "urn:replicon:project-list-filter:text"},
                "operatorUri": "urn:replicon:filter-operator:text-search",
                "rightExpression": {"value": {"text": text_search}},
            }
        payload = {
            "page": page,
            "pagesize": page_size,
            "columnUris": self.PROJECT_COLUMNS,
            "sort": None,
            "filterExpression": filter_expression,
        }
        return self._post("ProjectListService1", "GetData", payload)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    TASK_COLUMNS = [
        "urn:replicon:task-list-column:task",  # verified: valid column, returns empty rows when project has no tasks
    ]

    def get_tasks_for_project(self, project_uri: str, page: int = 1, page_size: int = 50) -> dict:
        """List tasks under a given project URI."""
        payload = {
            "page": page,
            "pagesize": page_size,
            "project": {"uri": project_uri},
            "columnUris": self.TASK_COLUMNS,
            "filterExpression": None,
            "hierarchyListDataOptionUris": None,
        }
        return self._post("TaskListService1", "GetHierarchyDataForProject", payload)

    # ------------------------------------------------------------------
    # Timesheet read
    # ------------------------------------------------------------------

    def get_timesheet_for_date(self, user_uri: str, date: dict) -> dict:
        """
        Get the full timesheet + details for a user covering the given date.
        date: {"year": YYYY, "month": M, "day": D}
        """
        payload = {
            "userUri": user_uri,
            "date": date,
            "timesheetGetOptionUri": None,
        }
        return self._post("TimesheetService1", "GetTimesheetDetailsForDate", payload)

    # ------------------------------------------------------------------
    # Timesheet write
    # ------------------------------------------------------------------

    def put_time_entry(self, user_uri: str, entry_date: dict, hours: float,
                        project_uri: str, task_uri: str | None = None,
                        comments: str = "", tuleap_ref: str = "") -> dict:
        """
        Add/update a single time entry. Uses TimeEntryService3.svc/PutTimeEntry —
        NOT PutStandardTimesheet2, which requires replacing the entire timesheet
        (confirmed via Replicon's own staff on their community forum) and is
        unsuitable for incremental entry. PutTimeEntry is the correct, current
        operation for this and has been verified end-to-end against a real
        write/read round-trip.

        entry_date: {"year": YYYY, "month": M, "day": D} (ints; verified API
        accepts both int and string forms for these, per working examples)

        project_uri: required. task_uri: optional — confirmed some projects
        (e.g. ones with no defined tasks) are allocated at the project level
        alone, with no task metadata entry at all.

        unitOfWorkId is a caller-generated idempotency key (uuid4), consistent
        with the convention confirmed elsewhere in this API.
        """
        custom_metadata = [
            {"keyUri": "urn:replicon:time-entry-metadata-key:project",
             "value": {"uri": project_uri}},
        ]
        if task_uri:
            custom_metadata.append({
                "keyUri": "urn:replicon:time-entry-metadata-key:task",
                "value": {"uri": task_uri},
            })
        if comments:
            custom_metadata.append({
                "keyUri": "urn:replicon:time-entry-metadata-key:comments",
                "value": {"text": comments},
            })

        # Tuleap reference — stored in extensionFieldValues (separate from customMetadata)
        extension_field_values = []
        if tuleap_ref:
            extension_field_values.append({
                "definition": {"uri": TULEAP_FIELD_URI},
                "textValue": tuleap_ref,
            })

        whole_hours = int(hours)
        minutes = round((hours - whole_hours) * 60)

        payload = {
            "timeEntry": {
                "target": {"parameterCorrelationId": str(uuid.uuid4())},
                "user": {"uri": user_uri},
                "entryDate": entry_date,
                "timeAllocationTypeUris": ["urn:replicon:time-allocation-type:project"],
                "interval": {
                    "hours": {
                        "hours": whole_hours,
                        "minutes": minutes,
                        "seconds": 0,
                        "milliseconds": 0,
                        "microseconds": 0,
                    },
                    "timePair": None,
                },
                "customMetadata": custom_metadata,
                "extensionFieldValues": extension_field_values,
            },
            "unitOfWorkId": str(uuid.uuid4()),
        }
        return self._post("TimeEntryService3", "PutTimeEntry", payload)

    def delete_time_entry(self, time_entry_uri: str) -> dict:
        """Delete a single time entry by its URI. Verified working."""
        return self._post("TimeEntryService3", "DeleteTimeEntry", {"timeEntryUri": time_entry_uri})

    def get_time_entries_for_date_range(self, user_uri: str, start_date: dict, end_date: dict) -> list:
        """
        Read time entries for a user over a date range. Verified working.
        Always returns a list — normalised here so callers never need to
        type-check the _post return value.
        """
        payload = {
            "user": {"uri": user_uri},
            "dateRange": {"startDate": start_date, "endDate": end_date},
            "asOf": None,
        }
        result = self._post("TimeEntryService3", "GetTimeEntriesForUserAndDateRange", payload)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Submit for approval
    # ------------------------------------------------------------------

    def submit_timesheet(self, timesheet_uri: str, current_status_uri: str,
                          comments: str = "", change_reason: str = "") -> dict:
        """
        unitOfWorkId is a caller-generated idempotency key (per Replicon's API
        convention, confirmed via their AuthenticationService docs) — not a
        server-issued token. Generated fresh per call so retries are detected
        as duplicates rather than double-submitted.

        current_status_uri must be fetched fresh from get_timesheet_for_date
        immediately before calling this — the caller (server.py) is responsible
        for that, so the status check reflects the true current state, not a
        stale value from earlier in the conversation.
        """
        if current_status_uri != TIMESHEET_STATUS_OPEN:
            raise TimesheetStateError(
                f"Cannot submit: timesheet status is '{current_status_uri}', "
                f"expected '{TIMESHEET_STATUS_OPEN}'. It may already be submitted "
                f"or in another state — re-check before retrying."
            )
        payload = {
            "timesheetUri": timesheet_uri,
            "unitOfWorkId": str(uuid.uuid4()),
            "comments": comments,
            "changeReason": change_reason,
        }
        return self._post("TimesheetApprovalService1", "Submit2", payload)

    # ------------------------------------------------------------------
    # User lookup
    # ------------------------------------------------------------------

    USER_LIST_COLUMNS = [
        "urn:replicon:user-list-column:user",  # confirmed via probe — same cell format as project list
    ]

    def find_users(self, name_search: str = "", page: int = 1, page_size: int = 25) -> dict:
        """
        Search for users by display name.

        Verified endpoint: UserListService1.svc/GetData
        Verified filter:   urn:replicon:user-list-filter:text
                           operator: urn:replicon:filter-operator:text-search
                           value:    {"text": "..."}
        Cell format: same as ProjectListService1 — cells[].uri + cells[].textValue
        """
        filter_expression = None
        if name_search:
            filter_expression = {
                "leftExpression": {
                    "filterDefinitionUri": "urn:replicon:user-list-filter:text"
                },
                "operatorUri": "urn:replicon:filter-operator:text-search",
                "rightExpression": {"value": {"text": name_search}},
            }
        payload = {
            "page": page,
            "pagesize": page_size,
            "columnUris": self.USER_LIST_COLUMNS,
            "sort": None,
            "filterExpression": filter_expression,
        }
        return self._post("UserListService1", "GetData", payload)

    # ------------------------------------------------------------------
    # Pending approvals (manager view)
    # ------------------------------------------------------------------

    # Column URIs confirmed via probe against live OLTPTimesheetListService1.
    # 'timesheet'       → URI + slug (slug encodes owner + period start as "{slug}/{Y}-{M}-{D}")
    # 'timesheet-owner' → owner URI + display name
    # 'approval-status' → human-readable approval status label
    TIMESHEET_LIST_COLUMNS = [
        "urn:replicon:timesheet-list-column:timesheet",
        "urn:replicon:timesheet-list-column:timesheet-owner",
        "urn:replicon:timesheet-list-column:approval-status",
    ]

    def get_pending_approvals_list(self, approver_uri: str,
                                    page: int = 1, page_size: int = 50) -> dict:
        """
        List timesheets currently waiting for approval by the given approver.

        Filter verified live (prior session):
          filterDefinitionUri = urn:replicon:timesheet-list-filter:currently-waiting-on-approver
          operatorUri         = urn:replicon:filter-operator:equal   (singular — not 'equals')
          rightExpression     = {"value": {"uri": approver_uri}}

        Note: the list response does NOT include a timesheet URI — only owner + period.
        Call get_timesheet_for_date per row to get the URI before approving.
        """
        payload = {
            "page": page,
            "pagesize": page_size,
            "columnUris": self.TIMESHEET_LIST_COLUMNS,
            "sort": None,
            "filterExpression": {
                "leftExpression": {
                    "filterDefinitionUri": (
                        "urn:replicon:timesheet-list-filter:currently-waiting-on-approver"
                    )
                },
                "operatorUri": "urn:replicon:filter-operator:equal",
                "rightExpression": {"value": {"uri": approver_uri}},
            },
        }
        return self._post("OLTPTimesheetListService1", "GetData", payload)

    # ------------------------------------------------------------------
    # Approve
    # ------------------------------------------------------------------

    def approve_timesheet(self, timesheet_uri: str, current_status_uri: str, comments: str = "") -> dict:
        """
        Approve a single timesheet. unitOfWorkId auto-generated (see submit_timesheet note).

        current_status_uri must be fetched fresh from get_timesheet_for_date
        immediately before calling this — confirmed real status for "awaiting
        approval" is TIMESHEET_STATUS_WAITING.
        """
        if current_status_uri != TIMESHEET_STATUS_WAITING:
            raise TimesheetStateError(
                f"Cannot approve: timesheet status is '{current_status_uri}', "
                f"expected '{TIMESHEET_STATUS_WAITING}' (submitted, awaiting approval). "
                f"It may still be open/draft, or already approved/rejected."
            )
        payload = {
            "timesheetUri": timesheet_uri,
            "unitOfWorkId": str(uuid.uuid4()),
            "comments": comments,
        }
        return self._post("TimesheetApprovalService1", "Approve", payload)

    def reopen_timesheet(self, timesheet_uri: str, current_status_uri: str,
                          comments: str = "", change_reason: str = "") -> dict:
        """
        Reopen a submitted timesheet (pull it back from 'waiting for approval'
        to 'open' so entries can be corrected).

        Operation confirmed via probe: TimesheetApprovalService1.svc/Reopen
        (HTTP 400 "Invalid Tenant Selected" on dummy URI = endpoint exists).

        current_status_uri must be TIMESHEET_STATUS_WAITING — caller fetches
        it fresh immediately before calling this.
        """
        if current_status_uri != TIMESHEET_STATUS_WAITING:
            raise TimesheetStateError(
                f"Cannot reopen: timesheet status is '{current_status_uri}', "
                f"expected '{TIMESHEET_STATUS_WAITING}' (submitted, awaiting approval). "
                f"Only submitted timesheets can be reopened."
            )
        payload = {
            "timesheetUri": timesheet_uri,
            "unitOfWorkId": str(uuid.uuid4()),
            "comments": comments,
            "changeReason": change_reason,
        }
        return self._post("TimesheetApprovalService1", "Reopen", payload)

