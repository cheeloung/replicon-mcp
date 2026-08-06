# Replicon MCP Server

Lets an AI agent (Claude Desktop, AnythingLLM, etc.) read your Replicon
timesheet, stage time entries as drafts, review them before anything is
sent, and push them to Replicon only when you explicitly say so. It can
also help review and approve timesheets pending your approval (manager
flow — still being finalized).

This guide assumes no prior technical experience. If you get stuck, check
the [Troubleshooting](#troubleshooting) section near the bottom — it covers
the most common problems people hit on Windows.

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

Follow these steps in order. Each one builds on the last — don't skip ahead.

### Step 1 — Check what you need

- **Python 3.10 or newer.** This project uses newer Python syntax, so an
  older Python (3.9 or earlier — common on some work laptops) will fail
  with a confusing `SyntaxError`. Step 3 below covers checking/installing it.
- **Your normal Replicon login** (the email/username and password you use
  to log into the Replicon website), or, if you're technical, a bearer
  token — both are covered in Step 6.
- **Claude Desktop** (or another MCP-compatible AI app) already installed.

### Step 2 — Get the code

**If you're not familiar with git (recommended for most people):**
1. Go to this project's GitHub page in your browser.
2. Click the green **Code** button, then **Download ZIP**.
3. Find the downloaded `.zip` file (usually in your Downloads folder) and
   extract it:
   - **Windows:** right-click the file → **Extract All...** → choose a
     simple location like `C:\Users\YourName\Documents\replicon-mcp` (avoid
     folders with unusual characters or very long paths).
   - **Mac:** double-click the file to extract it.
4. Remember exactly where you extracted it — you'll need this full path
   again in Step 7.

**If you're comfortable with git:**
```bash
git clone <this repository's URL>
```

### Step 3 — Install Python

Skip this step if you already have Python 3.10+ (check by opening a
terminal and running `python --version`, or `python3 --version` on Mac).

**Windows:**
1. Go to **python.org/downloads** and download the latest Python 3
   installer.
2. Run the installer. On the very first screen, **make sure to check the
   box "Add python.exe to PATH"** before clicking Install — this is the
   single most common thing people miss, and it causes the
   `'python' is not recognized` error later (see Troubleshooting).
3. Open a new **Command Prompt** or **PowerShell** window (search for it in
   the Start menu) and confirm it worked:
   ```
   python --version
   ```
   This should print something like `Python 3.12.x`. If it prints an error
   instead, see Troubleshooting.

**Mac:**
System Python on macOS is often too old (3.9, which this project can't
use). Install a current version via [Homebrew](https://brew.sh):
```bash
brew install python@3.11
```
Then use `python3.11` (or check `python3 --version` — if it's already 3.10+,
plain `python3` is fine).

### Step 4 — Open a terminal in the project folder

- **Windows:** open the folder you extracted in Step 2 in File Explorer,
  then type `cmd` into the address bar at the top and press Enter — this
  opens a Command Prompt already inside that folder. (Or hold Shift, right-click
  an empty spot in the folder, and choose "Open PowerShell window here".)
- **Mac:** open Terminal, then type `cd ` (with a trailing space) and drag
  the extracted folder into the Terminal window to fill in its path, then
  press Enter.

All the commands below assume you're in this folder.

### Step 5 — Create a virtual environment and install dependencies

A virtual environment keeps this project's Python packages separate from
everything else on your computer — it avoids version conflicts and means
you never need any special "override" flags.

**Windows (Command Prompt):**
```
python -m venv venv
venv\Scripts\activate.bat
pip install .
```

**Windows (PowerShell):**
```
python -m venv venv
venv\Scripts\Activate.ps1
pip install .
```
If `Activate.ps1` gives an error about running scripts being disabled, see
Troubleshooting — there's a one-line fix.

**Mac:**
```bash
python3.11 -m venv venv
source venv/bin/activate
pip install .
```

You'll know the virtual environment is active because your terminal prompt
will show `(venv)` at the start of the line. **Every time you reopen a
terminal to work with this project, you need to re-run the `activate`
command** (but you only need to run `pip install` once).

### Step 6 — Set up Replicon access

You need two things from Replicon: a way to authenticate, and your own
user URI. There are two ways to authenticate — pick one.

**Option A — Basic Auth (recommended, easiest):** just your normal Replicon
login. No extra setup needed — you'll fill in three values directly in
Step 7 (`.env` file):
- Your **company key** — look at the URL you use to log into Replicon,
  e.g. `https://eu1.replicon.com/Kranz/home/` → your company key is `Kranz`.
- Your Replicon **username** (usually your email) and **password**.

**Option B — Bearer token (advanced, optional):** a token that doesn't
require storing your password, and that you can revoke independently. This
needs a manual API call, so it's better suited to a technical teammate. Go
to your tenant's Security API docs page (replace `eu1` with your own
swimlane if different):
**https://eu1.replicon.com/services/docs/security.html**, and under
**Creating and revoking access tokens**, use
`POST /AuthenticationService1.svc/CreateAccessToken2` (you can omit the
`lifetime` field for a token with no expiry). If this sounds unfamiliar,
just use Option A instead — ask a technical teammate for help if you'd
rather not store your password.

### Step 7 — Configure `.env`

This file holds your Replicon credentials and never leaves your computer.

1. In the project folder, find `.env.example` and make a copy of it named
   exactly `.env` (no other text before or after the dot).
   - **Windows:** copy-paste the file, rename the copy to `.env`. **Important:**
     Windows hides file extensions by default, so if you create this file by
     hand (e.g. via Notepad's "Save As"), it can silently save as `.env.txt`
     instead of `.env` — Replicon will then act like the file doesn't exist.
     Turn on "File name extensions" in File Explorer's View tab so you can
     see and confirm the exact filename, or use the copy-and-rename approach
     above instead of Notepad's Save As.
   - **Mac:** in Terminal, `cp .env.example .env`.
2. Open `.env` in a plain text editor (Notepad, TextEdit, or VS Code) and
   fill in the values from Step 6 (either the three Option A fields, or
   `REPLICON_BEARER_TOKEN` for Option B — leave the ones you're not using
   blank or remove them).
3. Leave `REPLICON_USER_URI` for last. With the rest of `.env` filled in,
   run (inside your activated virtual environment):
   ```
   python find_my_user_uri.py "Your Name"
   ```
   This searches Replicon for your name and prints your user URI. Copy it
   into `REPLICON_USER_URI` in `.env`.

### Step 8 — Connect to Claude Desktop

Claude Desktop reads its MCP server list from a config file. Open (or
create) it:

- **Windows:** press `Win + R`, type `%APPDATA%\Claude`, press Enter, and
  open `claude_desktop_config.json` in a text editor (create the file if
  it doesn't exist).
- **Mac:** the file is at
  `~/Library/Application Support/Claude/claude_desktop_config.json`.

Add a `replicon` entry, pointing `command` at the **full path to the
python.exe/python3 inside the virtual environment you just created** — not
just `python` or `python3` — so Claude Desktop always uses the exact
environment where you installed the dependencies:

```json
{
  "mcpServers": {
    "replicon": {
      "command": "C:/Users/YourName/Documents/replicon-mcp/venv/Scripts/python.exe",
      "args": ["C:/Users/YourName/Documents/replicon-mcp/server.py"]
    }
  }
}
```

(On Mac, `command` would be
`/Users/YourName/replicon-mcp/venv/bin/python3` instead.)

Use forward slashes (`/`) in the paths even on Windows, as shown above —
this avoids having to escape backslashes in JSON. Replace `YourName` and
the folder path with your own from Step 2/4.

**AnythingLLM** — connect via SSE transport instead (see AnythingLLM's MCP
setup docs for the exact connection string format).

### Step 9 — Restart and verify

Fully quit and reopen Claude Desktop (not just close the window). Start a
new conversation and check that Replicon tools are available (Claude
Desktop typically shows a tool/plugin icon or count once an MCP server
connects successfully). Try asking it to show your timesheet for this
week. If nothing happens or you see an error, check
[Troubleshooting](#troubleshooting) below.

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
Each committed row also carries is_billable (true/false/null — null means
Replicon never computed it for that entry, seen on some older API-created
rows). Show it inline alongside the other fields, and use it to answer
billable-vs-non-client questions directly from get_my_timesheet /
get_team_member_timesheet without extra calls.

## Timesheet Entry — Draft First, Always
Never call push_drafts unless the user explicitly says to push, submit,
confirm, or equivalent.

Workflow:
1. Call stage_time_entry for each entry the user describes. Set is_billable:
   True for client project work, False for internal/non-client work (e.g.
   AI Learning, KWA General, Leave, other tasks/e-learning buckets). If the
   project isn't clearly one or the other, ask rather than guessing.
2. Show a formatted summary of all staged entries (project/task name, date,
   hours, tuleap ref, comments, billable flag).
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

## Submitting Timesheets
submit_timesheet writes directly to Replicon and is irreversible without a
reopen. Always show the user their staged/committed entries for the week via
get_my_timesheet and get explicit confirmation before calling submit_timesheet.
It handles submitting individual time entries automatically before submitting
the timesheet itself — no separate step needed.

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
- Entries pushed via this MCP always carry both "attendance" and "project"
  time-allocation types, matching UI-created entries — required for entries
  to appear in Replicon's standard timesheet report (confirmed 2026-08-05;
  entries missing "attendance" are silently excluded from that report even
  though fully committed/submittable).
- is_billable is a full read/write field, not just a push-time flag:
  stage_time_entry/edit_staged_entry set it on push, and get_my_timesheet/
  get_team_member_timesheet return it per row (read back from Replicon's
  is-billable custom metadata) — added 2026-08-05.

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

## Troubleshooting

**`'python' is not recognized as an internal or external command`**
Python wasn't added to your PATH during install. Reinstall Python from
python.org and make sure to check "Add python.exe to PATH" on the first
installer screen, or use the full path to `python.exe` instead of just
`python` everywhere in these instructions.

**`'pip' is not recognized as an internal or external command`**
Same PATH issue as above. As a workaround, replace `pip install ...` with
`python -m pip install ...`.

**`ModuleNotFoundError: No module named 'requests'`** (or `dotenv`, or `mcp`)
This means Claude Desktop is launching a *different* Python than the one
you ran `pip install` in — almost always because the config in Step 8
points at a bare `python`/`python3` instead of the virtual environment's own
interpreter. Double-check the `"command"` path in your Claude Desktop config
matches exactly `.../replicon-mcp/venv/Scripts/python.exe` (Windows) or
`.../replicon-mcp/venv/bin/python3` (Mac).

**`SyntaxError` pointing at something like `dict | None`**
Your Python is older than 3.10. Run `python --version` to check, and
install a newer Python (Step 3).

**PowerShell: "...cannot be loaded because running scripts is disabled on
this system"** (when activating the virtual environment)
Run this once in that PowerShell window, then try activating again:
```
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**Errors like "REPLICON_BASE_URL is not set in .env"**
Your `.env` file is missing, misnamed (check it's not `.env.txt` — see Step
7), or not in the same folder as `server.py`.

**Claude Desktop doesn't show any Replicon tools at all**
Usually a typo in the JSON config (a missing comma, or an unescaped
backslash in a Windows path — use forward slashes instead, as shown in
Step 8), or the config file is in the wrong location. Check the file is
valid JSON, and that you fully quit and reopened Claude Desktop after
editing it.

**401 / 403 errors when Claude tries to use a Replicon tool**
Your bearer token expired or was mistyped, or (for Basic Auth) your company
key, username, or password is wrong. Re-check the values in `.env` against
Step 6.

**Antivirus or company security software blocks `python.exe` from running**
Some corporate IT policies block scripts/executables run from a virtual
environment folder. Contact your IT department to allowlist the project
folder if this happens.

## Status

- ✅ Read timesheet, list projects/tasks, stage/edit/remove draft entries,
  push drafts to Replicon — verified working.
- ✅ Create a task under a project (create_task) — verified end-to-end against
  a live create/read/delete round-trip (uses the TaskService1 task-draft flow).
- ✅ Submit for approval (submit_timesheet) — verified end-to-end against a
  live tenant, including the required per-time-entry-revision-group submit
  step (TimeEntryRevisionGroupApprovalService1/Submit) before the
  timesheet-level submit (TimesheetApprovalService1/Submit2).
- ⏳ Approve timesheets — still not yet verified against a real write.
- 🐛→✅ Fixed 2026-08-05: entries pushed via put_time_entry were missing the
  "attendance" time-allocation type that UI-created entries always carry,
  making them invisible to Replicon's standard timesheet report despite
  being fully committed. Root-caused by comparing raw GetTimeEntriesFor...
  payloads for an API-created entry vs. a manually-duplicated UI entry with
  otherwise identical project/task/date/hours. Also added an explicit
  is_billable flag (stage_time_entry/edit_staged_entry) since API-created
  entries didn't reliably get Replicon's automatic billable/billing-rate
  computation either.
- ✅ is_billable read support added 2026-08-05: shape_time_entries
  (response_shapes.py) now extracts is-billable from each entry's
  customMetadata, so get_my_timesheet/get_team_member_timesheet return it
  per row — verified against live data (True for AMAG/ISL client rows,
  False for AI Learning/KWA General).
