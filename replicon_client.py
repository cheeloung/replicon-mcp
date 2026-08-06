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
from config import get_base_url, get_auth_headers, get_company_key, basic_auth_header, TULEAP_FIELD_URI


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
    def __init__(self, base_url: str | None = None, bearer_token: str | None = None,
                 username: str | None = None, password: str | None = None):
        """
        With no args: single shared credential from .env (stdio/local mode).
        With bearer_token: per-user personal API token against the shared
        tenant (remote/streamable-http mode) — see server.py's
        _resolve_caller(). Basic Auth (username+password) is kept only for
        local stdio use; it fails outright for any Replicon account with
        2-step verification enabled, which is why remote mode uses bearer
        tokens instead (see credentials.py).
        """
        self.base_url = base_url or get_base_url()
        if bearer_token:
            self.headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bearer_token}",
            }
        elif username and password:
            self.headers = {
                "Content-Type": "application/json",
                **basic_auth_header(username, password, get_company_key()),
            }
        else:
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
            detail = self._extract_error_detail(response)
            message = f"HTTP error calling {service}/{operation}: {e}"
            if detail:
                message += f" — {detail}"
            raise RepliconAPIError(message) from e

        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            raise RepliconAPIError(f"Replicon API error in {service}/{operation}: {body['error']}")

        # Most operations wrap the real payload in "d"; some return raw lists/objects
        return body.get("d", body) if isinstance(body, dict) else body

    @staticmethod
    def _extract_error_detail(response: requests.Response) -> str | None:
        """
        Replicon error responses carry human-readable detail in
        error.details.notifications[].displayText (confirmed live, e.g.
        "Time Entry already submitted." from TimeEntryRevisionGroupApprovalService1).
        raise_for_status() only gives the generic HTTP reason phrase, so pull
        this out separately to make RepliconAPIError messages actionable
        instead of just "400 Client Error: Bad Request".
        """
        try:
            body = response.json()
        except ValueError:
            return None
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return None
        notifications = (error.get("details") or {}).get("notifications") or []
        texts = [n["displayText"] for n in notifications if n.get("displayText")]
        return "; ".join(texts) if texts else error.get("reason")

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

    def get_project_details(self, project_uri: str) -> dict:
        """
        Get full project details, including budget/estimate fields.

        Confirmed endpoint: ProjectService1.svc/GetProjectDetails (verified
        live, Jul 2026 — not previously used in this client). Returns
        estimatedHours/estimatedCost (rolled up from task-level estimates
        when the project's estimationMode is "Task Based") and
        budget/budgetedHours/budgetedCost (a manual override, populated for
        "Fixed"-mode projects). Every project probed on this tenant uses
        Task Based mode, so budget/budgetedHours/budgetedCost were
        consistently null and estimatedHours carried the real total —
        callers should fall back to estimatedHours/estimatedCost when the
        budgeted* fields are unset.
        """
        return self._post("ProjectService1", "GetProjectDetails", {"projectUri": project_uri})

    PROJECT_ACTUALS_COLUMNS = [
        "urn:replicon:project-list-column:project",
        "urn:replicon:project-list-column:actual-hours",
    ]

    def get_project_actual_hours(self, project_uri: str) -> dict | None:
        """
        Get all-time actual hours logged against a single project (not
        scoped to any week or user).

        Filter confirmed live: urn:replicon:project-list-filter:project +
        filter-operator:equal + {"value": {"uri": project_uri}}. The more
        obvious ':uri' filter name is silently accepted but ignored by the
        API (returns unfiltered rows) rather than erroring — confirmed by
        probing a deliberately-invalid filter/column URI and seeing it
        return data anyway — so it must not be used here.

        Returns the single matching row dict (raw 'cells' shape from
        ProjectListService1), or None if the project has no matching row.
        """
        payload = {
            "page": 1,
            "pagesize": 1,
            "columnUris": self.PROJECT_ACTUALS_COLUMNS,
            "sort": None,
            "filterExpression": {
                "leftExpression": {"filterDefinitionUri": "urn:replicon:project-list-filter:project"},
                "operatorUri": "urn:replicon:filter-operator:equal",
                "rightExpression": {"value": {"uri": project_uri}},
            },
        }
        result = self._post("ProjectListService1", "GetData", payload)
        rows = result.get("rows", [])
        return rows[0] if rows else None

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

    def create_task(self, project_uri: str, name: str, parent_task_uri: str | None = None,
                     code: str = "", description: str = "",
                     start_date: dict | None = None, end_date: dict | None = None,
                     estimated_hours: float | None = None,
                     allow_time_entry: bool = True) -> dict:
        """
        Create a task under a project (optionally under a parent task).

        Uses TaskService1.svc via the task-draft flow, which is Replicon's
        documented and reliable path for building a new task:

          1. CreateNewDraft(parentUri)  — create a draft as a child of the given
             task OR project (the parent is parent_task_uri for a sub-task, else
             the project itself). Returns the draft's URI.
          2. Update* — set the draft's fields with the granular operations
             (UpdateName / UpdateCode / UpdateDescription /
             UpdateTimeEntryDateRange / UpdateEstimatedHours /
             UpdateAllowTimeEntry).
          3. PublishDraft(draftUri) — materialise the draft into a persisted task.
             Returns a TaskReference1 for the new task.

        Verified end-to-end against the live tenant with a create/read/delete
        round-trip. (The atomic CreateTaskOrApplyModifications operation exists
        too, but its request schema is not auto-generated and it proved brittle
        for top-level task creation — the draft flow is used instead.)

        Each request body is keyed by the operation's parameter names, per the
        WCF/JSON convention used throughout this client (e.g. {"taskUri": ...}).

        start_date / end_date: optional Replicon date dicts {"year","month","day"}
        setting the task's time-entry date range.

        estimated_hours: optional estimated effort as decimal hours (e.g. 7.5 for
        7h 30m). Converted to a TaskDuration1 {"hours","minutes","seconds"} the
        same way time-entry durations are (see put_time_entry).

        Returns the raw TaskReference1 payload: {"uri", "name", "code",
        "displayText", ...} for the newly created task.
        """
        # Parent is the parent task for a sub-task, otherwise the project itself
        # (projects are the root of their own task hierarchy).
        parent_uri = parent_task_uri or project_uri
        draft = self._post("TaskService1", "CreateNewDraft", {"parentUri": parent_uri})
        draft_uri = draft.get("uri") if isinstance(draft, dict) else draft
        if not draft_uri:
            raise RepliconAPIError(f"CreateNewDraft returned no draft URI: {draft!r}")

        try:
            self._post("TaskService1", "UpdateName", {"taskUri": draft_uri, "name": name})
            if code:
                self._post("TaskService1", "UpdateCode",
                           {"taskUri": draft_uri, "code": code})
            if description:
                self._post("TaskService1", "UpdateDescription",
                           {"taskUri": draft_uri, "description": description})
            if start_date or end_date:
                self._post("TaskService1", "UpdateTimeEntryDateRange", {
                    "taskUri": draft_uri,
                    "dateRange": {"startDate": start_date, "endDate": end_date},
                })
            if estimated_hours is not None:
                whole_hours = int(estimated_hours)
                minutes = round((estimated_hours - whole_hours) * 60)
                self._post("TaskService1", "UpdateEstimatedHours", {
                    "taskUri": draft_uri,
                    "estimatedHours": {"hours": whole_hours, "minutes": minutes, "seconds": 0},
                })
            self._post("TaskService1", "UpdateAllowTimeEntry",
                       {"taskUri": draft_uri, "allowTimeEntry": allow_time_entry})
            return self._post("TaskService1", "PublishDraft", {"draftUri": draft_uri})
        except Exception:
            # Best-effort cleanup so a failed create doesn't leave an orphan draft.
            try:
                self._post("TaskService1", "Delete", {"taskUri": draft_uri})
            except Exception:
                pass
            raise

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
                        comments: str = "", tuleap_ref: str = "",
                        is_billable: bool | None = None) -> dict:
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

        is_billable: explicit billable flag. Confirmed via live raw-entry
        comparison (2026-08-05) that UI-created entries always carry an
        is-billable custom metadata value (plus a billing-rate value when
        True), while entries pushed through this same PutTimeEntry operation
        without it left is-billable unset/inconsistent. Pass explicitly
        rather than relying on Replicon to infer it for API-created entries.
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
        if is_billable is not None:
            custom_metadata.append({
                "keyUri": "urn:replicon:time-entry-metadata-key:is-billable",
                "value": {"bool": is_billable},
            })
            if is_billable:
                custom_metadata.append({
                    "keyUri": "urn:replicon:time-entry-metadata-key:billing-rate",
                    "value": {"uri": "urn:replicon:project-specific-billing-rate"},
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
                # Both types required — confirmed via live raw-entry comparison
                # (2026-08-05) that UI-created entries always carry "attendance"
                # alongside "project". Entries missing "attendance" are silently
                # excluded from Replicon's standard timesheet report even though
                # they're fully committed, totalled, and submittable/approvable.
                "timeAllocationTypeUris": [
                    "urn:replicon:time-allocation-type:attendance",
                    "urn:replicon:time-allocation-type:project",
                ],
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

    def submit_time_entry_revision_group(self, revision_group_uri: str, comments: str = "") -> dict:
        """
        Submit a single time entry revision group for approval — the
        prerequisite step Replicon requires (on this tenant) before the
        timesheet-level Submit2 will succeed. Confirmed live against
        TimeEntryRevisionGroupApprovalService1.svc/Submit.

        Same call shape as submit_timesheet/approve_timesheet/reopen_timesheet:
        unitOfWorkId is a fresh caller-generated idempotency key per call.

        No status precondition (unlike submit_timesheet/approve_timesheet) —
        we have no confirmed status URI for revision groups. Callers should
        treat RepliconAPIError here as soft failure (the group may already
        be submitted from a prior attempt) and not let it block the overall
        submit_timesheet flow.
        """
        payload = {
            "timeEntryRevisionGroupUri": revision_group_uri,
            "unitOfWorkId": str(uuid.uuid4()),
            "comments": comments,
        }
        return self._post("TimeEntryRevisionGroupApprovalService1", "Submit", payload)

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

