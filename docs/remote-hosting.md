# Remote hosting (admin runbook)

This document is for whoever administers the shared `replicon-mcp` deployment
on the Kranz Wolfe org VM (`kwa-mcp01`, alongside `tuleap-mcp`) — it's not
needed for local/per-user usage (see the main [README](../README.md) for
that).

In this mode, one server instance is shared by the whole team. It
authenticates callers via Entra ID (so no shared secret exists), and each
person links their own personal Replicon username/password once via a
`/link` web page. Local stdio usage is unaffected — `TRANSPORT` defaults to
`stdio` and none of this applies unless it's explicitly set to
`streamable-http`.

## One-time setup

### 1. VM networking

- Add a DNS A record for `replicon-mcp.kranzwolfe.com` pointing at
  `kwa-mcp01`'s IP.
- Add a new Caddy site block on `kwa-mcp01` (alongside the existing
  `tuleap-mcp` one) that terminates TLS for that domain and forwards to
  `127.0.0.1:8001`, matching `docker-compose.yml`'s port binding — 8001, not
  tuleap-mcp's 8000, to avoid colliding on the same host.
- Confirm `https://replicon-mcp.kranzwolfe.com/health` returns
  `{"status": "ok"}` once the container is running — that's the fastest way
  to confirm the proxy path works before touching auth.

### 2. Azure App Registration (Entra ID)

Create a **new** App Registration for replicon-mcp in the Kranz Wolfe tenant
— do not reuse tuleap-mcp's. It serves two purposes: validating access
tokens Entra issues to the Claude connector, and acting as an OAuth client
for the `/link` sign-in page.

- Add **two** redirect URIs (not just one — this is easy to miss since the
  two purposes look similar):
  - `https://replicon-mcp.kranzwolfe.com/link/callback` (the `/link`
    self-service sign-in page)
  - `https://replicon-mcp.kranzwolfe.com/oauth/entra/callback` (the MCP
    OAuth proxy leg — `oauth_provider.py`'s `_entra_redirect_uri()`)
- Note the **Application (client) ID**, **Directory (tenant) ID**, and
  create a **client secret** — when creating the secret, copy the **Value**
  column, not the Secret ID. Mixing these up produces an `AADSTS7000215`
  error that looks unrelated to the actual mistake. These three become
  `ENTRA_CLIENT_ID`, `ENTRA_TENANT_ID`, `ENTRA_CLIENT_SECRET`.
- claude.ai's own connector redirect URI does **not** go into Entra at all —
  it goes into this server's `.env` as `MCP_CLIENT_REDIRECT_URIS` instead
  (see step 4). Entra has no Dynamic Client Registration (RFC 7591), so
  claude.ai's connector UI needs `ENTRA_CLIENT_ID` configured manually.
- **Verify before building anything else on top of this**: confirm the
  claude.ai custom-connector flow can actually complete OAuth against this
  app registration. Test end-to-end with a throwaway connector before
  relying on it for the team — the exact value for
  `MCP_CLIENT_REDIRECT_URIS` can only be confirmed by attempting to add the
  connector and reading it off the failed `/authorize` request.

### 3. Generate the credential-store encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the result in `.env` as `CREDENTIAL_STORE_KEY` (VM-local secret — losing
it means every linked Replicon credential becomes unrecoverable and everyone
has to re-link).

### 4. `.env` on the VM

Copy `.env.example` into `/opt/replicon-mcp/.env`. Keep `REPLICON_BASE_URL`
and `REPLICON_COMPANY_KEY` filled in as usual (shared across the whole
tenant). Fill in the "Remote/hosted mode only" section
(`TRANSPORT=streamable-http`, `PORT=8001`, `PUBLIC_URL`, the three
`ENTRA_*` vars, `MCP_CLIENT_REDIRECT_URIS`, `CREDENTIAL_STORE_KEY`). Leave
`REPLICON_USERNAME`/`PASSWORD`/`BEARER_TOKEN`/`USER_URI` unset — those are
only for local stdio use and are ignored in this mode.

### 5. Run it

```bash
docker compose up -d
```

Deployed in its own directory (`/opt/replicon-mcp/`) with its own
`docker-compose.yml` — no shared state with `tuleap-mcp` beyond the physical
host.

## Per-user onboarding

Each teammate:
1. Adds `https://replicon-mcp.kranzwolfe.com` as a custom Connector in
   claude.ai and completes the Entra login prompt it triggers.
2. Visits `https://replicon-mcp.kranzwolfe.com/link` once, signs in again
   (same Entra tenant), and enters their normal Replicon username/password
   plus their name as it appears in Replicon. The page searches Replicon for
   that name and asks them to confirm (or pick from) the match(es) — this
   extra step exists because Replicon has no direct "who am I" lookup, so
   the user's Replicon user URI can't be resolved from their login alone.

After that, their Claude sessions use their own Replicon identity
automatically — no local install, no shared credential.

## Operations

- **Revoke a user's Replicon link** (e.g. they changed their Replicon
  password, or offboarding): connect to the VM and run
  `python -c "import credentials; credentials.delete_replicon_credentials('<their-entra-oid>')"`
  inside the container. They'll need to visit `/link` again afterward.
- **Rotate `CREDENTIAL_STORE_KEY`**: this re-encrypts nothing automatically
  — rotating it invalidates every stored credential, so treat it as
  "everyone re-links," not a routine rotation.
- **Logs**: the server never logs Replicon passwords or Entra tokens — if
  you need to debug an auth failure, check for `CredentialsMissingError`
  (means the user hasn't visited `/link` yet) vs. a `None` from the token
  verifier (means the incoming bearer token itself failed validation —
  check `ENTRA_TENANT_ID`/`ENTRA_CLIENT_ID` match the app registration).
- **Container restart**: the OAuth authorization/access/refresh token state
  is in-memory only (see `oauth_provider.py`) — a restart means everyone
  needs to reauthenticate the MCP connector, though their `/link`ed Replicon
  credentials (persisted in SQLite under the `credential-store` volume)
  survive untouched.
