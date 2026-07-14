# Replicon MCP Server

Lets an AI agent (Claude Desktop, AnythingLLM, etc.) read your Replicon
timesheet, stage time entries as drafts, review them before anything is
sent, and push them to Replicon only when you explicitly say so. It can
also help review and approve timesheets pending your approval (manager
flow — still being finalized).

## Why staging instead of direct writes?

This server deliberately does **not** expose a "write directly to Replicon"
tool to the AI agent. Every entry goes through a local draft first:

```
You: "Log 2h on Project X, Thursday"
  → Agent checks the project exists, stages it locally (no Replicon call yet)

You: "Show my timesheet"
  → Agent always reads fresh from Replicon AND shows your local drafts,
    clearly labeled separately

You: "Push it"
  → ONLY now does anything get written to Replicon
```

This is intentional, not a limitation — it means you can freely add, edit,
or remove staged entries in conversation without touching your real
timesheet, and review everything before it becomes real.

## Setup

### 1. Get your Replicon Bearer Token

Go to your tenant's Security API docs page and generate an access token:

**https://eu1.replicon.com/services/docs/security.html**

(Replace `eu1` with your own swimlane if different — check your Replicon
login URL, e.g. `https://eu1.replicon.com/YourCompany/home/`.)

Under **Creating and revoking access tokens**, use
`POST /AuthenticationService1.svc/CreateAccessToken2`. You can omit the
`lifetime` field for a token with no expiry.

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

```
REPLICON_BASE_URL=https://eu1.replicon.com
REPLICON_BEARER_TOKEN=<paste your generated token here>
```

(Basic Auth fields are an optional fallback — only needed if you're not
using a bearer token.)

### 3. Install dependencies

```bash
pip install -r requirements.txt --break-system-packages
```

### 4. Connect to your AI agent

**Claude Desktop** — add to your MCP config (stdio transport):
```json
{
  "mcpServers": {
    "replicon": {
      "command": "python3",
      "args": ["/full/path/to/replicon-mcp/server.py"]
    }
  }
}
```

**AnythingLLM** — connect via SSE transport (see AnythingLLM's MCP setup
docs for the exact connection string format).

## AI Agent Prompt

If your AI client supports a system prompt or persistent instructions, paste
the following to teach it how to use this MCP correctly. This covers URI
resolution, the draft-first workflow, and timesheet retrieval for yourself and
your team.

```
# Replicon MCP — Operating Instructions

## Setup: Name Mapping File
On first use, ask the user where they'd like to store the project/task/user
name mapping file (e.g. "project-task-naming.md"). Default to the root of
your working directory if they have no preference. Read this file at the
start of every session before doing any Replicon work.

The file maps Replicon URIs to human-readable names. Use it to:
- Translate raw URIs into names when displaying timesheet data.
- Look up URIs when the user refers to a project or task by name.

## Resolving Unknown URIs
When a timesheet row contains a project or task URI not in the mapping file:
1. Call list_projects or list_tasks_for_project to resolve it.
2. Display the human-readable name to the user immediately.
3. Append the new entry to the mapping file under the correct section.

If the mapping file grows large (over ~300 lines), notify the user and
suggest they review and clean it up — some projects or tasks may no longer
be active.

Never show raw URIs to the user unless they specifically ask for them.

## Looking Up Projects and Tasks by Name
Before calling any list tool, check the mapping file first. Only call
list_projects or list_tasks_for_project if the name isn't already cached.
When the user refers to a project or task by partial name, fuzzy-match
against the file before making an API call.

## Retrieving Timesheets
Use a single mental model for timesheet retrieval — "get timesheet":
- If the user says "my timesheet" or doesn't specify a person, use
  get_my_timesheet.
- If the user names a team member (by name or role), look them up in the
  Users section of the mapping file to get their URI, then use
  get_team_member_timesheet with that URI.
- If a team member is not yet in the mapping file, use find_users to
  resolve their URI, then add them to the Users section.

Always display results with human-readable project/task names, not URIs.
Format daily hours as a Mon–Sun table. Show tuleap refs and comments inline.

## Timesheet Entry — Draft First, Always
Never call push_drafts unless the user explicitly says to push, submit,
confirm, or equivalent.

Workflow:
1. Call stage_time_entry for each entry the user describes.
2. Show a formatted summary of all staged entries (project/task name, date,
   hours, tuleap ref, comments).
3. Wait for explicit push confirmation before calling push_drafts.

If the user edits a staged entry, call edit_staged_entry and re-display the
full updated draft before asking again.

## Creating Tasks
create_task writes directly to Replicon — there is no draft/staging step for
tasks. Before calling it:
1. Resolve the target project URI with list_projects (or the mapping file).
2. Confirm the task name and target project with the user.
Then call create_task. It returns the new task's uri — add it to the mapping
file and use it when staging time entries. To create a sub-task, pass the
parent task's URI as parent_task_uri.

## Approval Workflow (Manager)
1. Call get_pending_approvals to list pending items.
2. For each, retrieve and display the full timesheet with human-readable
   names, hours per day, and tuleap refs.
3. Wait for explicit "approve [name]" confirmation before calling
   approve_timesheet.

## Key API Facts
- All URIs follow the pattern: urn:replicon-tenant:{tenant}:{type}:{id}
- Task-level entries: only task_uri is set; project_uri is null (the project
  is implied by the task).
- Project-level entries (no task): only project_uri is set.
- Weeks start on Monday.
- timesheet_status "open" = editable; "waiting" = submitted, pending approval.

## Future Integration Note
Tuleap reference numbers in timesheet entries will eventually link to Tuleap
artifact IDs via a separate tuleap-mcp. For now, store and display them as
plain numbers.
```

## Notes for teammates installing their own copy

- Each person runs their own instance with their own `.env` — credentials
  are never shared.
- `drafts.json` (created automatically) holds your staged, not-yet-pushed
  entries. It's local to your machine and not synced anywhere.
- If you use this from multiple apps (e.g. both Claude Desktop and
  AnythingLLM), each gets its own separate draft store — drafts staged in
  one won't appear in the other until pushed to Replicon.

## Status

- ✅ Read timesheet, list projects/tasks, stage/edit/remove draft entries,
  push drafts to Replicon — verified working.
- ✅ Create a task under a project (create_task) — verified end-to-end against
  a live create/read/delete round-trip (uses the TaskService1 task-draft flow).
- ⏳ Submit for approval / approve timesheets — built, not yet verified
  against a real write. Treat with extra caution until confirmed.
