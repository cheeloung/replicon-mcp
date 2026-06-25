"""
server.py — Replicon MCP server.

Exposes Replicon timesheet operations as MCP tools for AI agents (Claude
Desktop, etc.). All writes go through a local draft store first — the agent
can stage, review, and modify entries before anything touches Replicon.

Tools exposed:
  Read / browse
    get_my_timesheet          — my committed entries + pending drafts for a week
    list_projects             — search/list projects visible to me
    list_tasks_for_project    — list tasks under a project

  Draft management (local only, no Replicon writes)
    stage_time_entry          — add an entry to the local draft store
    edit_staged_entry         — update fields on a staged draft
    remove_staged_entry       — delete a staged draft

  Replicon writes (irreversible — always confirm with user first)
    push_drafts               — push all staged drafts to Replicon
    submit_timesheet          — submit my timesheet for approval
    approve_timesheet         — approve a team member's timesheet (manager)

  Manager / approval flow
    get_pending_approvals     — list timesheets waiting for my approval
    get_team_member_timesheet — view a team member's timesheet entries

Deliberately NOT exposed: raw put_time_entry (use stage → push instead).
"""

import json
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

import config
import draft_store
import timesheet_workflow
import response_shapes
from replicon_client import (
    RepliconClient,
    RepliconAPIError,
    TimesheetStateError,
    TIMESHEET_STATUS_OPEN,
)

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

config.validate()
_client = RepliconClient()
_my_uri = config.get_user_uri()

mcp = FastMCP(
    "Replicon Timesheet",
    instructions=(
        "You help users manage their Replicon timesheets. "
        "Always stage entries first and show the user a summary before pushing. "
        "For approvals, show the team member's entries and ask for explicit confirmation. "
        "Cache resolved URI→name mappings (projects, tasks, users) in conversation memory "
        "to avoid redundant lookups within the same session."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _week_bounds(date_str: str) -> tuple[dict, dict]:
    """
    Given any ISO date string (YYYY-MM-DD), return the Monday–Sunday bounds
    of that week as Replicon date dicts.

    Replicon uses Monday-start weeks (confirmed from live timesheet data).
    """
    d = date.fromisoformat(date_str)
    start = d - timedelta(days=d.weekday())  # Monday
    end = start + timedelta(days=6)           # Sunday
    return (
        {"year": start.year, "month": start.month, "day": start.day},
        {"year": end.year,   "month": end.month,   "day": end.day},
    )


def _date_dict(date_str: str) -> dict:
    """Convert 'YYYY-MM-DD' to {"year": int, "month": int, "day": int}."""
    d = date.fromisoformat(date_str)
    return {"year": d.year, "month": d.month, "day": d.day}


def _pretty(obj) -> str:
    """Compact JSON for returning structured data to the agent."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Read / browse tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_my_timesheet(week_date: str) -> str:
    """
    Read your timesheet for the week containing the given date.

    Returns committed entries (from Replicon, shaped into rows) plus any
    pending local drafts. Always reads fresh from Replicon — never stale.

    Args:
        week_date: Any date in the target week, as "YYYY-MM-DD".
                   The server computes the Monday–Sunday range automatically.

    Returns JSON with:
        timesheet_status  — current status URI (open / waiting / etc.)
        timesheet_uri     — URI needed for submit_timesheet
        committed_rows    — shaped time entry rows (grouped by row-number)
        drafts            — local staged entries not yet pushed
        week_start        — computed Monday of the week
        week_end          — computed Sunday of the week
    """
    week_start, week_end = _week_bounds(week_date)

    view = timesheet_workflow.get_timesheet_view(
        _client, _my_uri, week_start, week_end
    )

    shaped_rows = response_shapes.shape_time_entries(view["committed"])

    return _pretty({
        "week_start": week_start,
        "week_end": week_end,
        "timesheet_status": view["timesheet_status"],
        "timesheet_uri": view["timesheet_uri"],
        "committed_rows": shaped_rows,
        "total_committed_hours": round(
            sum(r["total_hours"] for r in shaped_rows), 4
        ),
        "drafts": view["drafts"],
    })


@mcp.tool()
def list_projects(text_search: str = "", page: int = 1, page_size: int = 50) -> str:
    """
    List projects visible to the authenticated user, optionally filtered by name.

    Use this to resolve a project name the user mentions into a project URI
    before staging an entry. Cache the URI→name mapping in conversation memory.

    Args:
        text_search: Optional name substring to filter results.
        page:        Page number (1-based).
        page_size:   Results per page (max 50 recommended).

    Returns shaped list:
        [ { "uri": "urn:replicon-tenant:...:project:70", "name": "KWA General" }, ... ]
    Cache uri→name in conversation memory to avoid re-fetching within the session.
    """
    raw = _client.get_projects(
        page=page,
        page_size=page_size,
        text_search=text_search or None,
    )
    return _pretty(response_shapes.shape_project_list(raw))


@mcp.tool()
def list_tasks_for_project(project_uri: str, page: int = 1, page_size: int = 50) -> str:
    """
    List tasks under a given project.

    Use this after resolving the project URI to find the task URI the user
    wants to log time against. Cache task URI→name mappings in conversation memory.

    Args:
        project_uri: The project's full Replicon URI.
        page:        Page number (1-based).
        page_size:   Results per page.

    Returns shaped list:
        [ { "uri": "urn:replicon-tenant:...:task:267", "name": "other tasks" }, ... ]
    Cache uri→name in conversation memory to avoid re-fetching within the session.
    """
    raw = _client.get_tasks_for_project(
        project_uri=project_uri,
        page=page,
        page_size=page_size,
    )
    return _pretty(response_shapes.shape_task_list(raw))


# ---------------------------------------------------------------------------
# Draft management tools (local only)
# ---------------------------------------------------------------------------

@mcp.tool()
def stage_time_entry(
    week_date: str,
    entry_date: str,
    hours: float,
    project_uri: str,
    project_name: str,
    task_uri: str = "",
    task_name: str = "",
    comments: str = "",
    tuleap_ref: str = "",
) -> str:
    """
    Stage a time entry locally. Nothing is sent to Replicon yet.

    Always call list_projects and list_tasks_for_project first to resolve URIs,
    and confirm the project/task with the user before staging.

    Args:
        week_date:    Any date in the target week ("YYYY-MM-DD"). Used to key
                      the draft store — must match the week you intend to push to.
        entry_date:   The specific day to log time on ("YYYY-MM-DD").
        hours:        Hours to log (decimal, e.g. 1.5 for 1h 30m).
        project_uri:  Full Replicon project URI.
        project_name: Human-readable project name (stored in draft for display).
        task_uri:     Full Replicon task URI (omit or pass "" if project-level only).
        task_name:    Human-readable task name (omit if no task).
        comments:     Optional time entry comment.
        tuleap_ref:   Optional Tuleap task reference (e.g. "2327"). Written to the
                      "Reference Tuleap" extension field in Replicon on push.

    Returns the created draft record including its draft_id (needed for edits/removes).
    """
    week_start, week_end = _week_bounds(week_date)
    entry_date_dict = _date_dict(entry_date)

    draft = timesheet_workflow.stage_entry(
        user_uri=_my_uri,
        week_start=week_start,
        week_end=week_end,
        entry_date=entry_date_dict,
        hours=hours,
        project_uri=project_uri,
        project_name=project_name,
        task_uri=task_uri or None,
        task_name=task_name or None,
        comments=comments,
        tuleap_ref=tuleap_ref,
    )
    return _pretty({"staged": draft})


@mcp.tool()
def edit_staged_entry(
    week_date: str,
    draft_id: str,
    hours: float | None = None,
    entry_date: str | None = None,
    comments: str | None = None,
    project_uri: str | None = None,
    project_name: str | None = None,
    task_uri: str | None = None,
    task_name: str | None = None,
) -> str:
    """
    Update one or more fields on a staged (not-yet-pushed) draft entry.

    Only pass the fields you want to change — unset fields are left unchanged.

    Args:
        week_date:  Any date in the target week ("YYYY-MM-DD").
        draft_id:   The draft_id returned when the entry was staged.
        hours:      New hours value.
        entry_date: New date ("YYYY-MM-DD").
        comments:   New comments text.
        project_uri/project_name/task_uri/task_name: New project or task.

    Returns the updated draft record, or an error if the draft_id was not found.
    """
    week_start, week_end = _week_bounds(week_date)

    changes = {}
    if hours is not None:
        changes["hours"] = hours
    if entry_date is not None:
        changes["entry_date"] = _date_dict(entry_date)
    if comments is not None:
        changes["comments"] = comments
    if project_uri is not None:
        changes["project_uri"] = project_uri
    if project_name is not None:
        changes["project_name"] = project_name
    if task_uri is not None:
        changes["task_uri"] = task_uri or None
    if task_name is not None:
        changes["task_name"] = task_name or None

    updated = draft_store.update_draft(
        _my_uri, week_start, week_end, draft_id, **changes
    )

    if updated is None:
        return _pretty({"error": f"Draft '{draft_id}' not found for this week."})
    return _pretty({"updated": updated})


@mcp.tool()
def remove_staged_entry(week_date: str, draft_id: str) -> str:
    """
    Remove a staged draft entry. No Replicon changes — local store only.

    Args:
        week_date: Any date in the target week ("YYYY-MM-DD").
        draft_id:  The draft_id of the entry to remove.

    Returns confirmation or an error if not found.
    """
    week_start, week_end = _week_bounds(week_date)

    removed = draft_store.remove_draft(_my_uri, week_start, week_end, draft_id)

    if not removed:
        return _pretty({"error": f"Draft '{draft_id}' not found for this week."})
    return _pretty({"removed": draft_id})


# ---------------------------------------------------------------------------
# Replicon write tools
# ---------------------------------------------------------------------------

@mcp.tool()
def push_drafts(week_date: str) -> str:
    """
    Push all pending staged entries for the given week to Replicon.

    Always show the user a summary of what will be pushed (via get_my_timesheet)
    and get explicit confirmation before calling this.

    Behavior:
      - Re-checks timesheet status fresh before pushing anything.
      - If the timesheet is not 'open', nothing is pushed (returns blocked=True).
      - Pushes each draft individually; partial success is possible.
      - Failed drafts remain staged with status='failed' for retry or removal.

    Args:
        week_date: Any date in the target week ("YYYY-MM-DD").

    Returns push results with succeeded/failed lists.
    """
    week_start, week_end = _week_bounds(week_date)

    result = timesheet_workflow.push_drafts(
        _client, _my_uri, week_start, week_end
    )
    return _pretty(result)


@mcp.tool()
def delete_committed_entry(week_date: str, entry_uri: str) -> str:
    """
    Delete a single committed time entry from Replicon.

    Use this when the user wants to remove an entry that has already been
    pushed. The entry URI comes from hour_entry_uris in get_my_timesheet output.

    Always show the user what will be deleted and get explicit confirmation
    before calling this — the action is irreversible.

    Args:
        week_date:  Any date in the target week ("YYYY-MM-DD"). Used to verify
                    the timesheet is still open before deleting.
        entry_uri:  The time entry URI to delete (from hour_entry_uris).

    Returns confirmation, or a clear error if the timesheet is not open.
    """
    week_start, _ = _week_bounds(week_date)

    details = _client.get_timesheet_for_date(_my_uri, week_start)
    current_status = details.get("timesheet", {}).get("statusUri")

    if current_status != TIMESHEET_STATUS_OPEN:
        return _pretty({
            "error": (
                f"Cannot delete: timesheet status is '{current_status}', not open. "
                "Reopen the timesheet before deleting entries."
            )
        })

    try:
        result = _client.delete_time_entry(entry_uri)
        return _pretty({"deleted": entry_uri, "response": result})
    except RepliconAPIError as e:
        return _pretty({"error": str(e)})


@mcp.tool()
def delete_committed_row(week_date: str, entry_uris: list[str]) -> str:
    """
    Delete all hour entries in a timesheet row (i.e. an entire project/task row).

    Pass the full hour_entry_uris list from a shaped row in get_my_timesheet.
    Each URI is deleted individually; partial success is possible if some fail.

    Always show the user which row (project/task + hours per day) will be
    removed and get explicit confirmation before calling this.

    Args:
        week_date:   Any date in the target week ("YYYY-MM-DD").
        entry_uris:  List of entry URIs to delete (the hour_entry_uris for the row).

    Returns count of deleted entries plus any failures.
    """
    week_start, _ = _week_bounds(week_date)

    details = _client.get_timesheet_for_date(_my_uri, week_start)
    current_status = details.get("timesheet", {}).get("statusUri")

    if current_status != TIMESHEET_STATUS_OPEN:
        return _pretty({
            "error": (
                f"Cannot delete: timesheet status is '{current_status}', not open. "
                "Reopen the timesheet before deleting entries."
            )
        })

    succeeded = []
    failed = []
    for uri in entry_uris:
        try:
            _client.delete_time_entry(uri)
            succeeded.append(uri)
        except RepliconAPIError as e:
            failed.append({"uri": uri, "error": str(e)})

    return _pretty({
        "deleted_count": len(succeeded),
        "succeeded": succeeded,
        "failed": failed,
    })


@mcp.tool()
def submit_timesheet(week_date: str, comments: str = "") -> str:
    """
    Submit your timesheet for the given week for approval.

    The timesheet must be in 'open' status — if it's already submitted or
    in another state, this will return an error. Fetch fresh status via
    get_my_timesheet immediately before calling this to confirm.

    CAUTION: This is a real write to Replicon. Confirm with the user first.

    Args:
        week_date: Any date in the target week ("YYYY-MM-DD").
        comments:  Optional submission comments.

    Returns the API response on success, or a clear error message.
    """
    week_start, _ = _week_bounds(week_date)

    # Fetch fresh timesheet details to get current status and URI
    details = _client.get_timesheet_for_date(_my_uri, week_start)
    timesheet = details.get("timesheet", {})
    timesheet_uri = timesheet.get("uri")
    current_status = timesheet.get("statusUri")

    if not timesheet_uri:
        return _pretty({"error": "Could not retrieve timesheet URI. Does a timesheet exist for this week?"})

    try:
        result = _client.submit_timesheet(
            timesheet_uri=timesheet_uri,
            current_status_uri=current_status,
            comments=comments,
        )
        return _pretty({"submitted": True, "response": result})
    except (TimesheetStateError, RepliconAPIError) as e:
        return _pretty({"error": str(e)})


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------

@mcp.tool()
def find_users(name_search: str, page: int = 1, page_size: int = 25) -> str:
    """
    Search for Replicon users by display name.

    Use this to resolve a team member's name to their user URI before calling
    get_team_member_timesheet or approve_timesheet. Cache uri→name mappings
    in conversation memory for the session.

    Args:
        name_search: Name substring to search for (e.g. "Lim" or "Seay Ee").
        page:        Page number (1-based).
        page_size:   Results per page (default 25).

    Returns shaped list:
        [ { "uri": "urn:replicon-tenant:...:user:105", "name": "Lim, Seay Ee" }, ... ]
    """
    raw = _client.find_users(name_search=name_search, page=page, page_size=page_size)
    return _pretty(response_shapes.shape_user_list(raw))


# ---------------------------------------------------------------------------
# Manager / approval flow tools
# ---------------------------------------------------------------------------

@mcp.tool()
def reopen_timesheet(week_date: str, comments: str = "") -> str:
    """
    Reopen your submitted timesheet so you can correct entries before approval.

    The timesheet must be in 'waiting for approval' status. If it has already
    been approved or is still open, this will return a clear error.

    CAUTION: This is a real write to Replicon. Confirm with the user first.

    Args:
        week_date: Any date in the target week ("YYYY-MM-DD").
        comments:  Optional comment explaining why you're reopening.

    Returns confirmation on success, or a clear error if status has changed.
    """
    week_start, _ = _week_bounds(week_date)

    details = _client.get_timesheet_for_date(_my_uri, week_start)
    timesheet = details.get("timesheet", {})
    timesheet_uri = timesheet.get("uri")
    current_status = timesheet.get("statusUri")

    if not timesheet_uri:
        return _pretty({"error": "Could not retrieve timesheet URI. Does a timesheet exist for this week?"})

    try:
        result = _client.reopen_timesheet(
            timesheet_uri=timesheet_uri,
            current_status_uri=current_status,
            comments=comments,
        )
        return _pretty({"reopened": True, "response": result})
    except (TimesheetStateError, RepliconAPIError) as e:
        return _pretty({"error": str(e)})


@mcp.tool()
def get_pending_approvals() -> str:
    """
    List all timesheets currently waiting for your approval.

    For each result, returns the owner's name/URI, the week period, and
    a timesheet_uri pre-fetched for you (needed by approve_timesheet).

    If there are many pending timesheets, use get_team_member_timesheet to
    drill into a specific person's entries before approving.

    Returns a list of pending approval items. Empty list = nothing pending.
    """
    raw_list = _client.get_pending_approvals_list(_my_uri)
    items = response_shapes.shape_pending_approvals_list(raw_list)

    # Enrich each item with the timesheet URI (requires a secondary call per item)
    enriched = []
    for item in items:
        period_start = item["period_start"]
        if not period_start:
            enriched.append({**item, "timesheet_uri": None, "timesheet_status": None})
            continue

        try:
            details = _client.get_timesheet_for_date(item["owner_uri"], period_start)
            ts = details.get("timesheet", {})
            enriched.append({
                **item,
                "timesheet_uri": ts.get("uri"),
                "timesheet_status": ts.get("statusUri"),
            })
        except RepliconAPIError as e:
            enriched.append({
                **item,
                "timesheet_uri": None,
                "timesheet_status": None,
                "lookup_error": str(e),
            })

    return _pretty({
        "pending_count": len(enriched),
        "pending": enriched,
    })


@mcp.tool()
def get_team_member_timesheet(user_uri: str, week_date: str) -> str:
    """
    View a team member's timesheet entries for a given week.

    Use the owner_uri from get_pending_approvals and the period_start as week_date.
    The entries are shaped the same way as get_my_timesheet for consistency.

    Args:
        user_uri:  The team member's full Replicon user URI.
        week_date: Any date in their target week ("YYYY-MM-DD").

    Returns shaped time entry rows + timesheet metadata.
    """
    week_start, week_end = _week_bounds(week_date)

    raw_entries = _client.get_time_entries_for_date_range(user_uri, week_start, week_end)
    details = _client.get_timesheet_for_date(user_uri, week_start)
    ts = details.get("timesheet", {})

    shaped = response_shapes.shape_pending_approval_full(
        owner_uri=user_uri,
        owner_name=ts.get("user", {}).get("displayText", user_uri),
        period_start=week_start,
        period_end=week_end,
        timesheet_uri=ts.get("uri", ""),
        timesheet_status=ts.get("statusUri", ""),
        raw_entries=raw_entries,
    )
    return _pretty(shaped)


@mcp.tool()
def approve_timesheet(
    timesheet_uri: str,
    owner_uri: str,
    week_date: str,
    comments: str = "",
) -> str:
    """
    Approve a team member's timesheet.

    Always review the entries first via get_team_member_timesheet and get
    explicit confirmation from the user before calling this.

    The server re-fetches current timesheet status immediately before approving
    to guard against race conditions (someone else may have acted on it).

    CAUTION: This is irreversible. Confirm with the user before calling.

    Args:
        timesheet_uri: The timesheet URI from get_pending_approvals.
        owner_uri:     The team member's user URI from get_pending_approvals.
        week_date:     Any date in their week ("YYYY-MM-DD") — used to fetch fresh status.
        comments:      Optional approval comment.

    Returns confirmation on success, or a clear error if status has changed.
    """
    week_start, _ = _week_bounds(week_date)

    # Re-fetch status fresh — do not trust a cached value
    try:
        details = _client.get_timesheet_for_date(owner_uri, week_start)
        current_status = details.get("timesheet", {}).get("statusUri")
    except RepliconAPIError as e:
        return _pretty({"error": f"Could not verify current timesheet status: {e}"})

    try:
        result = _client.approve_timesheet(
            timesheet_uri=timesheet_uri,
            current_status_uri=current_status,
            comments=comments,
        )
        return _pretty({"approved": True, "response": result})
    except (TimesheetStateError, RepliconAPIError) as e:
        return _pretty({"error": str(e)})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
