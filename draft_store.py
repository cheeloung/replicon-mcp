"""
draft_store.py — Local JSON-backed staging area for time entries not yet
pushed to Replicon.

Design (per sign-off):
- Drafts persist to disk (drafts.json, next to this file) so they survive
  a server restart.
- Each app/session gets its OWN drafts.json (no shared cross-app path) —
  confirmed acceptable, simplifies this considerably.
- Keyed by user URI, then by week range string, so multiple users could
  theoretically share a server instance without collision (even though
  cross-app sharing was explicitly ruled out, cross-user collision within
  one file is still worth avoiding cheaply).
- Caller (server.py) is responsible for merging this with a fresh Replicon
  read before showing anything to the user — this module does NOT talk to
  Replicon at all, by design, to keep concerns separated.
"""

import json
import uuid
from pathlib import Path
from datetime import datetime, timezone
from threading import Lock

_STORE_PATH = Path(__file__).parent / "drafts.json"
_lock = Lock()  # guard against concurrent read-modify-write within one process


def _week_key(start_date: dict, end_date: dict) -> str:
    """Build a stable key like '2026-06-15_2026-06-21' from date dicts."""
    s = f"{start_date['year']:04d}-{start_date['month']:02d}-{start_date['day']:02d}"
    e = f"{end_date['year']:04d}-{end_date['month']:02d}-{end_date['day']:02d}"
    return f"{s}_{e}"


def _load() -> dict:
    if not _STORE_PATH.exists():
        return {}
    try:
        with open(_STORE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable file — fail safe to empty rather than crash.
        # This means a corrupted drafts.json silently loses drafts; flagging
        # this tradeoff rather than hiding it.
        return {}


def _save(data: dict) -> None:
    with open(_STORE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def add_draft(user_uri: str, week_start: dict, week_end: dict, entry_date: dict,
              hours: float, project_uri: str, project_name: str,
              task_uri: str | None = None, task_name: str | None = None,
              comments: str = "", tuleap_ref: str = "",
              is_billable: bool | None = None) -> dict:
    """Stage a new draft entry. Returns the created draft record."""
    with _lock:
        data = _load()
        week_key = _week_key(week_start, week_end)
        data.setdefault(user_uri, {}).setdefault(week_key, [])

        draft = {
            "draft_id": str(uuid.uuid4()),
            "entry_date": entry_date,
            "hours": hours,
            "project_uri": project_uri,
            "project_name": project_name,
            "task_uri": task_uri,
            "task_name": task_name,
            "comments": comments,
            "tuleap_ref": tuleap_ref,  # "Reference Tuleap" extension field value
            "is_billable": is_billable,  # None = let Replicon infer; True/False = explicit
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "draft",  # draft -> pushed | failed
        }
        data[user_uri][week_key].append(draft)
        _save(data)
        return draft


def update_draft(user_uri: str, week_start: dict, week_end: dict,
                  draft_id: str, **changes) -> dict | None:
    """Update fields on an existing draft. Returns updated record, or None if not found."""
    with _lock:
        data = _load()
        week_key = _week_key(week_start, week_end)
        drafts = data.get(user_uri, {}).get(week_key, [])
        for draft in drafts:
            if draft["draft_id"] == draft_id:
                draft.update(changes)
                _save(data)
                return draft
        return None


def remove_draft(user_uri: str, week_start: dict, week_end: dict, draft_id: str) -> bool:
    """Remove a draft entry. Returns True if removed, False if not found."""
    with _lock:
        data = _load()
        week_key = _week_key(week_start, week_end)
        drafts = data.get(user_uri, {}).get(week_key, [])
        original_len = len(drafts)
        data.setdefault(user_uri, {})[week_key] = [
            d for d in drafts if d["draft_id"] != draft_id
        ]
        if len(data[user_uri][week_key]) == original_len:
            return False
        _save(data)
        return True


def get_drafts(user_uri: str, week_start: dict, week_end: dict) -> list[dict]:
    """Get all draft entries for a user's given week (any status)."""
    data = _load()
    week_key = _week_key(week_start, week_end)
    return data.get(user_uri, {}).get(week_key, [])


def mark_pushed(user_uri: str, week_start: dict, week_end: dict, draft_id: str,
                 replicon_time_entry_uri: str) -> None:
    """Mark a draft as successfully pushed, recording the resulting Replicon URI."""
    update_draft(user_uri, week_start, week_end, draft_id,
                 status="pushed", replicon_time_entry_uri=replicon_time_entry_uri,
                 pushed_at=datetime.now(timezone.utc).isoformat())


def mark_failed(user_uri: str, week_start: dict, week_end: dict, draft_id: str,
                 error_message: str) -> None:
    """Mark a draft as failed to push, recording the error for the user to see."""
    update_draft(user_uri, week_start, week_end, draft_id,
                 status="failed", error_message=error_message)


def clear_pushed_drafts(user_uri: str, week_start: dict, week_end: dict) -> int:
    """
    Remove drafts already marked 'pushed' from the store (cleanup after a
    successful push cycle). Returns count removed. Failed drafts are left
    in place for the user to retry or remove manually.
    """
    with _lock:
        data = _load()
        week_key = _week_key(week_start, week_end)
        drafts = data.get(user_uri, {}).get(week_key, [])
        remaining = [d for d in drafts if d.get("status") != "pushed"]
        removed_count = len(drafts) - len(remaining)
        if user_uri in data and week_key in data[user_uri]:
            data[user_uri][week_key] = remaining
            _save(data)
        return removed_count
