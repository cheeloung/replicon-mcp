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
    create_task               — create a task under a project (direct write)
    push_drafts               — push all staged drafts to Replicon
    submit_timesheet          — submit my timesheet for approval
    approve_timesheet         — approve a team member's timesheet (manager)

  Manager / approval flow
    get_pending_approvals     — list timesheets waiting for my approval
    get_team_member_timesheet — view a team member's timesheet entries

Deliberately NOT exposed: raw put_time_entry (use stage → push instead).
"""

import json
import os
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings

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

TRANSPORT = os.getenv("TRANSPORT", "stdio")

_INSTRUCTIONS = (
    "You help users manage their Replicon timesheets. "
    "Always stage entries first and show the user a summary before pushing. "
    "For approvals, show the team member's entries and ask for explicit confirmation. "
    "Cache resolved URI→name mappings (projects, tasks, users) in conversation memory "
    "to avoid redundant lookups within the same session."
)


class CredentialsMissingError(Exception):
    """Raised inside a tool call — never sys.exit(), which would kill the
    shared server process for every connected user, not just this request."""


def _resolve_caller() -> tuple[RepliconClient, str]:
    """
    Returns (client, user_uri) for the caller of the current tool invocation.

    stdio mode: unchanged, single shared credential from .env, resolved once.
    streamable-http mode: each request is authenticated as a specific Entra
    user (see oauth_provider.py); look up *their* linked Replicon credentials.
    """
    if TRANSPORT == "stdio":
        config.validate()
        return RepliconClient(), config.get_user_uri()

    access_token = get_access_token()
    creds = credentials.get_replicon_credentials(access_token.subject)
    if creds is None:
        public_url = os.environ.get("PUBLIC_URL", "")
        raise CredentialsMissingError(
            f"No Replicon account linked yet for this user. Visit {public_url}/link to link one."
        )
    bearer_token, user_uri = creds
    return RepliconClient(config.get_base_url(), bearer_token=bearer_token), user_uri


# Set in _build_mcp() when TRANSPORT != stdio — referenced by the
# /oauth/entra/callback route registered below.
_oauth_provider = None


def _build_mcp() -> FastMCP:
    global _oauth_provider

    if TRANSPORT == "stdio":
        config.validate()
        return FastMCP("Replicon Timesheet", instructions=_INSTRUCTIONS)

    from oauth_provider import provider_from_env

    public_url = os.environ["PUBLIC_URL"].rstrip("/")
    _oauth_provider = provider_from_env()
    return FastMCP(
        "Replicon Timesheet",
        instructions=_INSTRUCTIONS,
        # We act as the full OAuth Authorization Server here (not just a
        # resource server delegating to Entra) because claude.ai's connector
        # implementation expects /authorize and /token on this same domain —
        # see oauth_provider.py for why and how it proxies Entra underneath.
        auth_server_provider=_oauth_provider,
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=public_url,
        ),
        # claude.ai's connector treats whatever bare URL you type into "Add
        # custom connector" as both the resource identifier AND the actual
        # MCP protocol endpoint — it doesn't append a path of its own. Move
        # the endpoint to root (FastMCP defaults to /mcp) to match, or every
        # connector attempt 404s trying to speak MCP at the bare domain.
        streamable_http_path="/",
        # Binds all interfaces intentionally: runs inside a container behind a
        # reverse proxy that terminates TLS (see docs/), never exposed directly.
        host="0.0.0.0",  # nosec B104
        port=int(os.getenv("PORT", "8001")),
    )


mcp = _build_mcp()

if TRANSPORT != "stdio":
    import credentials
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

    import onboarding

    onboarding.register(mcp)

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/oauth/entra/callback", methods=["GET"])
    async def oauth_entra_callback(request: Request):
        redirect_url = _oauth_provider.complete_entra_login(dict(request.query_params))
        if redirect_url is None:
            return HTMLResponse(
                "Login session expired or invalid — go back and try connecting again.",
                status_code=400,
            )
        return RedirectResponse(redirect_url)


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
def get_my_timesheet(week_date: str, include_project_budget: bool = True) -> str:
    """
    Read your timesheet for the week containing the given date.

    Returns committed entries (from Replicon, shaped into rows) plus any
    pending local drafts. Always reads fresh from Replicon — never stale.

    Args:
        week_date: Any date in the target week, as "YYYY-MM-DD".
                   The server computes the Monday–Sunday range automatically.
        include_project_budget: When True (default), also fetch a "used vs
                   total" hours budget summary for each distinct project on
                   the timesheet (2 extra read-only API calls per distinct
                   project). Set False to skip and speed up the call.

    Returns JSON with:
        timesheet_status  — current status URI (open / waiting / etc.)
        timesheet_uri     — URI needed for submit_timesheet
        committed_rows    — shaped time entry rows (grouped by row-number when
                            present, else by project+task with row_number=null)
        drafts            — local staged entries not yet pushed
        week_start        — computed Monday of the week
        week_end          — computed Sunday of the week
        project_budgets   — (when include_project_budget) {project_uri: {
                            estimation_mode, budgeted_hours, actual_hours,
                            hours_remaining, percent_used}, ...} — actual_hours
                            is the project's all-time total, not just this week
    """
    _client, _my_uri = _resolve_caller()
    week_start, week_end = _week_bounds(week_date)

    view = timesheet_workflow.get_timesheet_view(
        _client, _my_uri, week_start, week_end
    )

    shaped_rows = response_shapes.shape_time_entries(view["committed"])

    result = {
        "week_start": week_start,
        "week_end": week_end,
        "timesheet_status": view["timesheet_status"],
        "timesheet_uri": view["timesheet_uri"],
        "committed_rows": shaped_rows,
        "total_committed_hours": round(
            sum(r["total_hours"] for r in shaped_rows), 4
        ),
        "drafts": view["drafts"],
    }
    if include_project_budget:
        result["project_budgets"] = timesheet_workflow.get_project_budgets_for_rows(
            _client, shaped_rows
        )
    return _pretty(result)


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
    _client, _ = _resolve_caller()
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
    _client, _ = _resolve_caller()
    raw = _client.get_tasks_for_project(
        project_uri=project_uri,
        page=page,
        page_size=page_size,
    )
    return _pretty(response_shapes.shape_task_list(raw))


@mcp.tool()
def create_task(
    project_uri: str,
    name: str,
    parent_task_uri: str = "",
    code: str = "",
    description: str = "",
    start_date: str = "",
    end_date: str = "",
    estimated_hours: float = 0,
    allow_time_entry: bool = True,
) -> str:
    """
    Create a new task under an existing project in Replicon.

    This writes directly to Replicon (there is no staging step for tasks). Always
    resolve the project URI with list_projects first, and confirm the task name
    and target project with the user before calling — creating a task is a real,
    visible change to the project structure.

    Args:
        project_uri:      Full Replicon project URI the task belongs to (required).
        name:             Task name (required).
        parent_task_uri:  Optional parent task URI, to create a sub-task. Omit or
                          pass "" to create a top-level task under the project.
        code:             Optional task code.
        description:      Optional task description.
        start_date:       Optional time-entry start date ("YYYY-MM-DD").
        end_date:         Optional time-entry end date ("YYYY-MM-DD").
        estimated_hours:  Optional estimated effort in decimal hours (e.g. 7.5 for
                          7h 30m). Omit or pass 0 to leave the estimate unset.
        allow_time_entry: Whether time can be logged against the task (default True).

    On success returns the new task:
        { "uri": "...:task:NNN", "name": "...", "code": "...", "display_text": "..." }
    Use the returned uri when staging time entries. You can call
    list_tasks_for_project again to confirm the task now appears.

    Returns a structured { "error": ... } if Replicon rejects the request (e.g.
    the project URI is not found).
    """
    _client, _ = _resolve_caller()
    try:
        raw = _client.create_task(
            project_uri=project_uri,
            name=name,
            parent_task_uri=parent_task_uri or None,
            code=code,
            description=description,
            start_date=_date_dict(start_date) if start_date else None,
            end_date=_date_dict(end_date) if end_date else None,
            estimated_hours=estimated_hours if estimated_hours else None,
            allow_time_entry=allow_time_entry,
        )
    except RepliconAPIError as e:
        return _pretty({"error": str(e)})

    return _pretty(response_shapes.shape_created_task(raw))


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
    is_billable: bool | None = None,
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
        is_billable:  Explicit billable flag (True/False). Client project work
                      should be True; internal/non-client work (e.g. AI Learning,
                      KWA General) should be False. Omit to leave unset.

    Returns the created draft record including its draft_id (needed for edits/removes).
    """
    _, _my_uri = _resolve_caller()
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
        is_billable=is_billable,
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
    is_billable: bool | None = None,
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
        is_billable: New billable flag (True/False).

    Returns the updated draft record, or an error if the draft_id was not found.
    """
    _, _my_uri = _resolve_caller()
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
    if is_billable is not None:
        changes["is_billable"] = is_billable

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
    _, _my_uri = _resolve_caller()
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
    _client, _my_uri = _resolve_caller()
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
    _client, _my_uri = _resolve_caller()
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
    _client, _my_uri = _resolve_caller()
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

    Replicon requires every underlying time entry to be individually
    submitted before the timesheet itself can be submitted. This tool does
    that automatically — submitting each time entry revision group for the
    week, then the timesheet itself — mirroring the "Submit X time entry(s)"
    + "Submit timesheet" flow in the Replicon web UI. Nothing else needs to
    be called first.

    The timesheet must be in 'open' status — if it's already submitted or
    in another state, this returns an error. Fetch fresh status via
    get_my_timesheet immediately before calling this to confirm.

    CAUTION: Real writes to Replicon (one per time entry revision group,
    then the timesheet). Confirm with the user first.

    Args:
        week_date: Any date in the target week ("YYYY-MM-DD").
        comments:  Optional submission comments (applied to both the
                   entry-level and timesheet-level submits).

    Returns which revision groups were submitted/failed plus the
    timesheet-level API response, or a clear error message.
    """
    _client, _my_uri = _resolve_caller()
    week_start, week_end = _week_bounds(week_date)

    try:
        result = timesheet_workflow.submit_timesheet(
            _client, _my_uri, week_start, week_end, comments=comments
        )
        return _pretty({"submitted": True, **result})
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
    _client, _ = _resolve_caller()
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
    _client, _my_uri = _resolve_caller()
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

    For each result, returns the owner's name/URI, the week period,
    the timesheet_uri (needed by approve_timesheet), and the approval status.

    Use get_team_member_timesheet to drill into a specific person's entries
    before approving.

    Returns a list of pending approval items. Empty list = nothing pending.
    """
    _client, _my_uri = _resolve_caller()
    raw_list = _client.get_pending_approvals_list(_my_uri)
    items = response_shapes.shape_pending_approvals_list(raw_list)
    return _pretty({"pending_count": len(items), "pending": items})


@mcp.tool()
def get_team_member_timesheet(
    user_uri: str, week_date: str, include_project_budget: bool = True
) -> str:
    """
    View a team member's timesheet entries for a given week.

    Use the owner_uri from get_pending_approvals and the period_start as week_date.
    The entries are shaped the same way as get_my_timesheet for consistency.

    Args:
        user_uri:  The team member's full Replicon user URI.
        week_date: Any date in their target week ("YYYY-MM-DD").
        include_project_budget: When True (default), also fetch a "used vs
                   total" hours budget summary for each distinct project on
                   the timesheet (2 extra read-only API calls per distinct
                   project). Set False to skip and speed up the call.

    Returns shaped time entry rows + timesheet metadata, plus (when
    include_project_budget) a project_budgets dict keyed by project_uri with
    {estimation_mode, budgeted_hours, actual_hours, hours_remaining,
    percent_used} — actual_hours is the project's all-time total, not just
    this week.
    """
    _client, _ = _resolve_caller()
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
    if include_project_budget:
        shaped["project_budgets"] = timesheet_workflow.get_project_budgets_for_rows(
            _client, shaped["rows"]
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
    _client, _ = _resolve_caller()
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

def main():
    mcp.run(transport="stdio" if TRANSPORT == "stdio" else "streamable-http")


if __name__ == "__main__":
    main()
