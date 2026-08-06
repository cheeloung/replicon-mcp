"""
response_shapes.py — Clean, agent-readable output from raw Replicon API responses.

These functions take raw API payloads and return structured dicts suitable for
returning from MCP tools. The goal is to hide Replicon's internal wire format
from the AI agent and give it only what it needs to reason and act.

Grouping rule for time entries (verified against live API, Jun–Jul 2026):
  - Entries created through the Replicon UI carry a
    `urn:replicon:widget-ui-metadata-key:row-number` metadata value. Where
    present, this is the canonical grouping key — one row-number = one visual
    row in the Replicon timesheet UI.
  - Entries created via the API (our own TimeEntryService3/PutTimeEntry pushes)
    carry NO row-number — it is UI widget metadata that Replicon does not
    assign server-side. These entries are grouped by (project_uri, task_uri)
    into synthetic rows with row_number=None instead of being dropped.
  - Entries with interval=null are PLACEHOLDER records: the row is allocated for
    that week but has no hours on that day. They are skipped when computing hours
    but their URIs are still tracked (needed for delete operations).
  - The SAME project/task combination can legitimately appear in MULTIPLE
    row-number groups — users can add the same task on separate occasions,
    creating distinct rows. row-number is authoritative; do NOT de-duplicate by
    project+task URI.
  - Entries that have a task URI but no project URI: the task itself belongs to
    a project, but the project is not redundantly embedded in each entry. The
    AI agent should resolve task URI → name (and parent project) via
    list_tasks / list_projects and cache the mapping in conversation memory.
"""

from collections import defaultdict
from config import TULEAP_FIELD_URI


# ---------------------------------------------------------------------------
# Time entry shaping (my timesheet + team member view)
# ---------------------------------------------------------------------------

def _parse_hours(interval: dict | None) -> float | None:
    """Extract decimal hours from an interval dict. Returns None for null/empty."""
    if not interval:
        return None
    h = interval.get("hours")
    if h is None:
        return None
    total = h.get("hours", 0) + h.get("minutes", 0) / 60 + h.get("seconds", 0) / 3600
    return round(total, 4) if total else None


def _duration_to_hours(d: dict | None) -> float | None:
    """
    Convert a {"hours", "minutes", "seconds", ...} duration dict to decimal
    hours. Used for project budget/estimate fields (estimatedHours,
    budgetedHours, the actual-hours list column's calendarDayDurationValue).

    Distinct from _parse_hours (used for time-entry intervals, where a total
    of 0 means "no entry" and is normalised to None): here 0.0 is a
    meaningful budget/actual value in its own right — only a missing dict
    means "not set".
    """
    if d is None:
        return None
    total = d.get("hours", 0) + d.get("minutes", 0) / 60 + d.get("seconds", 0) / 3600
    return round(total, 4)


def _date_key(entry_date: dict) -> str:
    """Convert {year, month, day} dict to 'YYYY-MM-DD' string."""
    return f"{entry_date['year']}-{entry_date['month']:02d}-{entry_date['day']:02d}"


def shape_time_entries(raw_entries: list[dict]) -> list[dict]:
    """
    Group raw GetTimeEntriesForUserAndDateRange entries into one dict per
    timesheet row.

    Grouping key: row-number metadata where present (UI-created entries);
    (project_uri, task_uri) otherwise (API-created entries, which carry no
    row-number — see module docstring). Numbered rows sort first, then
    synthetic rows by project/task URI.

    Input: the 'd' array from TimeEntryService3/GetTimeEntriesForUserAndDateRange.

    Output: list of row dicts:
    {
        "row_number": float | None, # Replicon's row identifier; None for
                                    # rows grouped from API-created entries
        "project_uri": str | None,  # set when entry is at project level (no task)
        "task_uri":    str | None,  # set when entry is at task level
        "is_billable": bool | None, # None if Replicon never computed/stored it
                                    # for this entry (seen on some older
                                    # API-created rows pushed before the
                                    # is-billable fix landed — see put_time_entry)
        "daily_hours": {            # only days with actual hours (null/zero excluded)
            "2026-06-15": 1.0,
            "2026-06-19": 1.0,
        },
        "total_hours": float,
        "all_entry_uris": [str, ...],   # all entry URIs in group (incl. placeholders)
        "hour_entry_uris": [str, ...],  # only URIs where hours > 0 (for targeted delete)
    }

    Rows with 0 total hours are included — they represent allocated rows with no
    time logged this week (still visible as empty rows in the UI).
    """
    # grouping key (see below) → accumulated row data
    rows: dict[tuple, dict] = defaultdict(lambda: {
        "row_number": None,
        "project_uri": None,
        "task_uri": None,
        "is_billable": None,
        "daily_hours": {},
        "daily_comments": {},    # only days with a non-empty comment
        "daily_tuleap_refs": {},  # only days with a non-empty Tuleap reference
        "total_hours": 0.0,
        "all_entry_uris": [],
        "hour_entry_uris": [],
    })

    for entry in raw_entries:
        # Extract metadata into a flat dict keyed by the last segment of the keyUri
        meta = {
            m["keyUri"].split(":")[-1]: m["value"]
            for m in entry.get("customMetadata", [])
        }

        row_num = meta.get("row-number", {}).get("number")
        if row_num is not None:
            key = ("row", row_num)
        else:
            # API-created entry (no widget row-number) — group by project+task.
            # Kept separate from numbered rows: row-number is authoritative
            # where present, and the same project/task can span multiple rows.
            key = (
                "api",
                meta.get("project", {}).get("uri"),
                meta.get("task", {}).get("uri"),
            )

        row = rows[key]
        row["row_number"] = row_num

        # project_uri and task_uri are consistent across all entries in a group;
        # take the first non-None value we encounter.
        if row["project_uri"] is None and "project" in meta:
            row["project_uri"] = meta["project"].get("uri")
        if row["task_uri"] is None and "task" in meta:
            row["task_uri"] = meta["task"].get("uri")
        if row["is_billable"] is None and "is-billable" in meta:
            row["is_billable"] = meta["is-billable"].get("bool")

        entry_uri = entry.get("uri")
        if entry_uri:
            row["all_entry_uris"].append(entry_uri)

        date = entry.get("entryDate", {})
        if not date:
            continue

        date_str = _date_key(date)
        hours = _parse_hours(entry.get("interval"))

        if hours is not None and hours > 0:
            # Real hours — accumulate
            existing = row["daily_hours"].get(date_str, 0.0)
            row["daily_hours"][date_str] = round(existing + hours, 4)
            row["total_hours"] = round(row["total_hours"] + hours, 4)
            if entry_uri:
                row["hour_entry_uris"].append(entry_uri)

            # Comments — per day, per row (only on entries with actual hours)
            comment = meta.get("comments", {}).get("text", "").strip()
            if comment:
                row["daily_comments"][date_str] = comment

            # Tuleap reference — from extensionFieldValues, matched by definition URI
            for ext in entry.get("extensionFieldValues", []):
                if (ext.get("definition", {}).get("uri") == TULEAP_FIELD_URI
                        and ext.get("textValue", "").strip()):
                    row["daily_tuleap_refs"][date_str] = ext["textValue"].strip()
                    break
        # else: placeholder entry (interval=null or 0h) — tracked via all_entry_uris only

    # Stable output order: numbered rows first (by row_number), then
    # synthetic API rows (by project/task URI)
    return sorted(
        rows.values(),
        key=lambda r: (r["row_number"] is None, r["row_number"] or 0,
                       r["project_uri"] or "", r["task_uri"] or ""),
    )


# ---------------------------------------------------------------------------
# Project and task list shaping
# ---------------------------------------------------------------------------

def _cells_to_item(cells: list[dict]) -> dict | None:
    """
    Extract the first object cell from a list-service row.

    Live API response format (confirmed against ProjectListService1):
      cells[i] = { "uri": "urn:...", "textValue": "KWA General",
                   "dataType": "urn:replicon:list-type:object", "slug": "...", ... }

    Note: the list service uses 'cells' (not 'rowData') and embeds uri/textValue
    directly on the cell — there is no nested 'value' object.
    """
    for cell in cells:
        uri = cell.get("uri", "")
        name = cell.get("textValue", "")
        if uri:
            return {"uri": uri, "name": name}
    return None


def shape_project_list(raw_response: dict) -> list[dict]:
    """
    Shape raw ProjectListService1/GetData response into a clean list.

    Input: unwrapped response dict (the 'd' key already stripped by _post).
    Output:
        [
            { "uri": "urn:replicon-tenant:...:project:70", "name": "KWA General" },
            ...
        ]
    """
    rows = raw_response.get("rows", [])
    result = []
    for row in rows:
        item = _cells_to_item(row.get("cells", []))
        if item:
            result.append(item)
    return result


def shape_task_list(raw_response: dict) -> list[dict]:
    """
    Shape raw TaskListService1/GetHierarchyDataForProject response into a clean list.

    Same cell format as project list. Output:
        [
            { "uri": "urn:replicon-tenant:...:task:267", "name": "other tasks" },
            ...
        ]
    """
    rows = raw_response.get("rows", [])
    result = []
    for row in rows:
        item = _cells_to_item(row.get("cells", []))
        if item:
            result.append(item)
    return result


def shape_created_task(raw_response: dict) -> dict:
    """
    Shape the raw TaskService1/CreateTaskOrApplyModifications response into a
    clean dict.

    The operation returns a TaskReference1 for the new task (unwrapped from "d"
    by RepliconClient._post). Confirmed shape:
      { "uri": "...:task:NNN", "name": "...", "code": "...", "displayText": "..." }

    Output:
        { "uri": "...:task:NNN", "name": "...", "code": "...", "display_text": "..." }
    """
    ref = raw_response if isinstance(raw_response, dict) else {}
    return {
        "uri": ref.get("uri"),
        "name": ref.get("name"),
        "code": ref.get("code"),
        "display_text": ref.get("displayText"),
    }


def shape_project_budget(project_uri: str, details: dict, actuals_row: dict | None) -> dict:
    """
    Hours-based "used vs total" budget summary for one project, combining
    ProjectService1/GetProjectDetails (total) with a single ProjectListService1
    actual-hours row (used, all-time — not scoped to any week or user).

    budgeted_hours prefers the manual budgetedHours override (set on
    "Fixed"-mode projects) and falls back to estimatedHours (the "Task
    Based" rollup from task-level estimates) when unset — see
    RepliconClient.get_project_details for why both exist.

    All fields are None when the project has no budget data at all, rather
    than showing misleading zeros.

    Output:
    {
        "project_uri": str,
        "estimation_mode": str | None,   # e.g. "Task Based" — for context
        "budgeted_hours": float | None,
        "actual_hours": float | None,
        "hours_remaining": float | None, # budgeted - actual; negative = over budget
        "percent_used": float | None,    # actual / budgeted * 100, 1 decimal
    }
    """
    budgeted_hours = _duration_to_hours(details.get("budgetedHours"))
    if budgeted_hours is None:
        budgeted_hours = _duration_to_hours(details.get("estimatedHours"))

    actual_hours = None
    if actuals_row:
        for cell in actuals_row.get("cells", []):
            if cell.get("dataType") == "urn:replicon:list-type:calendar-day-duration":
                actual_hours = _duration_to_hours(cell.get("calendarDayDurationValue"))
                break

    hours_remaining = (
        round(budgeted_hours - actual_hours, 4)
        if budgeted_hours is not None and actual_hours is not None else None
    )
    percent_used = (
        round(actual_hours / budgeted_hours * 100, 1)
        if actual_hours is not None and budgeted_hours else None
    )

    return {
        "project_uri": project_uri,
        "estimation_mode": (details.get("estimationMode") or {}).get("displayText"),
        "budgeted_hours": budgeted_hours,
        "actual_hours": actual_hours,
        "hours_remaining": hours_remaining,
        "percent_used": percent_used,
    }


def shape_user_list(raw_response: dict) -> list[dict]:
    """
    Shape raw UserListService1/GetData response into a clean list.

    Cell format confirmed via probe — identical to project/task lists:
      cells[i] = { "uri": "urn:...:user:9", "textValue": "Cheah, Chee Loung", ... }

    Output:
        [
            { "uri": "urn:replicon-tenant:...:user:9", "name": "Cheah, Chee Loung" },
            ...
        ]
    """
    rows = raw_response.get("rows", [])
    result = []
    for row in rows:
        item = _cells_to_item(row.get("cells", []))
        if item:
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Pending approvals shaping (manager view)
# ---------------------------------------------------------------------------

def _parse_period_from_slug(slug: str) -> tuple[dict | None, dict | None]:
    """
    Parse the period start (and derived end) from an OLTPTimesheetListService1
    timesheet cell slug.

    Slug format confirmed from live API: "{owner-slug}/{year}-{month}-{day}"
    Example: ".seayeelimkranzwolfecom/2026-6-15"

    Returns (period_start, period_end) as date dicts, or (None, None) on failure.
    """
    from datetime import date, timedelta
    try:
        date_part = slug.rsplit("/", 1)[-1]  # "2026-6-15"
        y, m, d = (int(x) for x in date_part.split("-"))
        start = date(y, m, d)
        end = start + timedelta(days=6)
        return (
            {"year": start.year, "month": start.month, "day": start.day},
            {"year": end.year,   "month": end.month,   "day": end.day},
        )
    except (ValueError, IndexError):
        return None, None


def shape_pending_approvals_list(raw_list_response: dict) -> list[dict]:
    """
    Shape the raw OLTPTimesheetListService1/GetData response into a clean list
    of pending approval items.

    Cell format confirmed via probe (Jun 2026): rows use cells[] keyed by
    objectType (no columnUri on the cell). Three columns requested and their
    confirmed objectTypes:
      urn:replicon:object-type:timesheet       → URI + slug ("{owner}/{Y}-{M}-{D}")
      urn:replicon:object-type:user            → owner URI + textValue name
      urn:replicon:object-type:approval-status → textValue label

    The timesheet URI is available directly in the list — no secondary
    get_timesheet_for_date call needed for the approval workflow.

    Output: list of:
    {
        "owner_uri":       str,
        "owner_name":      str,
        "timesheet_uri":   str,
        "period_start":    {"year": int, "month": int, "day": int} | None,
        "period_end":      {"year": int, "month": int, "day": int} | None,
        "approval_status": str | None,
    }
    """
    rows = raw_list_response.get("rows", [])

    results = []
    for row in rows:
        cells = row.get("cells", [])

        # Match cells by objectType — order is not guaranteed
        timesheet_cell = None
        owner_cell = None
        status_cell = None
        for cell in cells:
            obj = cell.get("objectType", "")
            if obj == "urn:replicon:object-type:timesheet":
                timesheet_cell = cell
            elif obj == "urn:replicon:object-type:user":
                owner_cell = cell
            elif obj == "urn:replicon:object-type:approval-status":
                status_cell = cell

        if not timesheet_cell or not owner_cell:
            continue  # malformed row — skip rather than crash

        period_start, period_end = _parse_period_from_slug(
            timesheet_cell.get("slug", "")
        )

        results.append({
            "owner_uri": owner_cell.get("uri", ""),
            "owner_name": owner_cell.get("textValue", ""),
            "timesheet_uri": timesheet_cell.get("uri", ""),
            "period_start": period_start,
            "period_end": period_end,
            "approval_status": (
                status_cell.get("textValue") if status_cell else None
            ),
        })

    return results


def shape_pending_approval_full(
    owner_uri: str,
    owner_name: str,
    period_start: dict,
    period_end: dict,
    timesheet_uri: str,
    timesheet_status: str,
    raw_entries: list[dict],
) -> dict:
    """
    Full pending approval item including shaped time entries.
    Used when the manager wants detail on a specific person's timesheet,
    not just the summary list.

    Output:
    {
        "owner_uri":        str,
        "owner_name":       str,
        "period_start":     {"year": ..., "month": ..., "day": ...},
        "period_end":       {"year": ..., "month": ..., "day": ...},
        "timesheet_uri":    str,
        "timesheet_status": str,
        "rows":             [...],  # shaped time entry rows (same format as shape_time_entries)
        "total_hours":      float,
    }
    """
    shaped_rows = shape_time_entries(raw_entries)
    total = round(sum(r["total_hours"] for r in shaped_rows), 4)

    return {
        "owner_uri": owner_uri,
        "owner_name": owner_name,
        "period_start": period_start,
        "period_end": period_end,
        "timesheet_uri": timesheet_uri,
        "timesheet_status": timesheet_status,
        "rows": shaped_rows,
        "total_hours": total,
    }
