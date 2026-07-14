"""
timesheet_workflow.py — Orchestrates replicon_client (live API) and
draft_store (local staging) into the higher-level operations the MCP
tools actually expose.

This module owns the rule: any "view timesheet" operation ALWAYS reads
fresh from Replicon and merges with local drafts — never shows
drafts-only or Replicon-only by accident.
"""

from replicon_client import (
    RepliconClient,
    RepliconAPIError,
    TimesheetStateError,
    TIMESHEET_STATUS_OPEN,
)
import draft_store


def get_timesheet_view(client: RepliconClient, user_uri: str,
                        week_start: dict, week_end: dict) -> dict:
    """
    Fresh Replicon read + local draft overlay, clearly separated.

    Returns:
        {
            "committed": [...],   # real entries already in Replicon
            "drafts": [...],      # staged, not yet pushed (status: draft/failed)
            "timesheet_status": "urn:replicon:timesheet-status:open" | ...
        }
    """
    committed_entries = client.get_time_entries_for_date_range(user_uri, week_start, week_end)
    timesheet_details = client.get_timesheet_for_date(user_uri, week_start)
    current_status = timesheet_details.get("timesheet", {}).get("statusUri")

    all_drafts = draft_store.get_drafts(user_uri, week_start, week_end)
    # Don't show already-pushed drafts as if they're still pending —
    # they're now part of "committed" (the fresh Replicon read above).
    pending_drafts = [d for d in all_drafts if d.get("status") in ("draft", "failed")]

    return {
        "committed": committed_entries,
        "drafts": pending_drafts,
        "timesheet_status": current_status,
        "timesheet_uri": timesheet_details.get("timesheet", {}).get("uri"),
    }


def stage_entry(user_uri: str, week_start: dict, week_end: dict,
                 entry_date: dict, hours: float,
                 project_uri: str, project_name: str,
                 task_uri: str | None = None, task_name: str | None = None,
                 comments: str = "", tuleap_ref: str = "") -> dict:
    """
    Stage a new entry locally. No Replicon call at all — pure local write.
    Caller (server.py) is responsible for having already resolved
    project_uri/task_uri via client.get_projects()/get_tasks_for_project()
    and confirmed the choice with the user before calling this.
    """
    return draft_store.add_draft(
        user_uri, week_start, week_end, entry_date, hours,
        project_uri, project_name, task_uri, task_name, comments, tuleap_ref,
    )


def push_drafts(client: RepliconClient, user_uri: str,
                 week_start: dict, week_end: dict) -> dict:
    """
    Push all pending (draft/failed) entries for the given week to Replicon.

    Behavior (per sign-off):
    - Re-checks timesheet status fresh BEFORE pushing anything. If not
      'open', hard stop — no partial push, surface to user for intervention.
    - If open: pushes each draft one at a time via put_time_entry.
    - Continues through individual failures (partial success allowed).
    - Successfully pushed drafts are cleared from the store.
    - Failed drafts remain in the store (status='failed', with error
      message) for the user to retry or remove.

    Returns:
        {
            "blocked": bool,              # True if status check failed everything
            "block_reason": str | None,
            "succeeded": [...],           # list of draft dicts that pushed OK
            "failed": [...],              # list of {draft, error} that failed
        }
    """
    timesheet_details = client.get_timesheet_for_date(user_uri, week_start)
    current_status = timesheet_details.get("timesheet", {}).get("statusUri")

    if current_status != "urn:replicon:timesheet-status:open":
        return {
            "blocked": True,
            "block_reason": (
                f"Timesheet status is '{current_status}', not open. "
                f"It may have been submitted or modified elsewhere since "
                f"these entries were staged. Please review before retrying."
            ),
            "succeeded": [],
            "failed": [],
        }

    pending = [d for d in draft_store.get_drafts(user_uri, week_start, week_end)
               if d.get("status") in ("draft", "failed")]

    succeeded = []
    failed = []

    for draft in pending:
        try:
            result = client.put_time_entry(
                user_uri=user_uri,
                entry_date=draft["entry_date"],
                hours=draft["hours"],
                project_uri=draft["project_uri"],
                task_uri=draft.get("task_uri"),
                comments=draft.get("comments", ""),
                tuleap_ref=draft.get("tuleap_ref", ""),
            )
            time_entry_uri = result.get("uri")
            draft_store.mark_pushed(user_uri, week_start, week_end,
                                     draft["draft_id"], time_entry_uri)
            succeeded.append(draft)
        except Exception as e:
            draft_store.mark_failed(user_uri, week_start, week_end,
                                     draft["draft_id"], str(e))
            failed.append({"draft": draft, "error": str(e)})

    draft_store.clear_pushed_drafts(user_uri, week_start, week_end)

    return {
        "blocked": False,
        "block_reason": None,
        "succeeded": succeeded,
        "failed": failed,
    }


def submit_timesheet(client: RepliconClient, user_uri: str,
                      week_start: dict, week_end: dict,
                      comments: str = "") -> dict:
    """
    Submit the week's timesheet for approval — submitting each underlying
    time entry revision group first. Mirrors the "Submit X time entry(s)"
    + "Submit timesheet" two-button flow in the Replicon web UI, which this
    tenant requires (see replicon_client.submit_time_entry_revision_group).

    - Fetches timesheet details fresh (status + uri).
    - If status is 'open', collects distinct revisionGroupUri values from
      this week's committed entries (get_time_entries_for_date_range — the
      field is already present on every raw entry) and submits each one.
      Per-group failure is soft: recorded, doesn't stop the rest or block
      the timesheet-level submit that follows.
    - Always attempts the existing client.submit_timesheet() afterwards;
      its TimesheetStateError/RepliconAPIError propagate unchanged.

    Returns:
        {
            "revision_groups_submitted": [uri, ...],
            "revision_groups_failed": [{"uri": ..., "error": ...}, ...],
            "revision_groups_total": int,
            "timesheet_response": dict,   # raw Submit2 response
        }
    """
    timesheet_details = client.get_timesheet_for_date(user_uri, week_start)
    timesheet = timesheet_details.get("timesheet", {})
    timesheet_uri = timesheet.get("uri")
    current_status = timesheet.get("statusUri")

    if not timesheet_uri:
        raise TimesheetStateError(
            "Could not retrieve timesheet URI. Does a timesheet exist for this week?"
        )

    revision_groups_submitted = []
    revision_groups_failed = []

    if current_status == TIMESHEET_STATUS_OPEN:
        entries = client.get_time_entries_for_date_range(user_uri, week_start, week_end)
        revision_group_uris = sorted({
            e["revisionGroupUri"] for e in entries if e.get("revisionGroupUri")
        })
        for uri in revision_group_uris:
            try:
                client.submit_time_entry_revision_group(uri, comments=comments)
                revision_groups_submitted.append(uri)
            except RepliconAPIError as e:
                revision_groups_failed.append({"uri": uri, "error": str(e)})

    timesheet_response = client.submit_timesheet(
        timesheet_uri=timesheet_uri,
        current_status_uri=current_status,
        comments=comments,
    )

    return {
        "revision_groups_submitted": revision_groups_submitted,
        "revision_groups_failed": revision_groups_failed,
        "revision_groups_total": len(revision_groups_submitted) + len(revision_groups_failed),
        "timesheet_response": timesheet_response,
    }
