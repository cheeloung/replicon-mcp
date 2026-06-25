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

## Best practice: caching URI → name lookups in conversation memory

Replicon's API refers to projects, tasks, and users by opaque URIs (e.g.
`urn:replicon-tenant:...:project:5611`) with no embedded name. If your AI
agent calls `get_projects` / `get_tasks_for_project` / a user lookup every
time it needs to resolve a URI it's already seen this session, that's wasted
calls and slower responses — for no real benefit, since these names rarely
change mid-conversation.

Rather than building a caching layer into the server itself (which would
risk silently serving a stale name for a renamed or archived project), ask
your AI agent to hold this mapping in its own conversation memory instead.
Add an instruction like this early in your session (or as a persistent
instruction, if your client supports it):

> When you resolve a Replicon project, task, or user URI to a human-readable
> name using this MCP server, remember that name for the rest of this
> conversation. Before calling a lookup tool to resolve a URI you've already
> resolved, check your memory first instead of calling again. Likewise, if I
> refer to a project or task by name, check whether you already have its URI
> before searching for it again. If a name looks off, missing, or
> inconsistent with what you'd expect, re-fetch it rather than trusting a
> stale memory.

This keeps a session fast without the server itself caching anything that
could silently go out of date. Memory resets between sessions, so the first
mention of any given project/task/user each session still costs one real
lookup — that's expected, and it's what guarantees you're never working off
outdated names.

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
- ⏳ Submit for approval / approve timesheets — built, not yet verified
  against a real write. Treat with extra caution until confirmed.
