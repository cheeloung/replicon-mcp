"""The one-time '/link' web flow: a user signs in with Entra ID, then enters
their personal Replicon API (bearer) token so future MCP calls made under
their Entra identity can use their own Replicon credentials.

Bearer token, not username/password: Basic Auth fails outright (401) for any
Replicon account with 2-step verification enabled — confirmed live against
this tenant. A personal API token generated per
https://eu1.replicon.com/services/docs/security.html (the tenant's own
domain — the generic sb1.replicon.com sandbox docs page does NOT work for
generating a token here) sidesteps MFA entirely, so the /link form walks
the user through generating one via that page's Swagger UI.

Unlike tuleap-mcp's onboarding (a single URL + API key form), Replicon has no
"who am I" endpoint — the same gap find_my_user_uri.py works around locally
by searching for yourself by name. So this flow has an extra step: after
collecting the token, it searches Replicon for the display name the user
typed and has them confirm (or pick from) the match(es) before storing
anything.

This is a separate OAuth *client* flow (we are the Relying Party logging the
browser in) from oauth_provider.py's EntraProxyOAuthProvider (which validates
tokens Claude presents to us as a Resource Server) — the two use the same
Entra app registration but serve different purposes.
"""

import os
import html

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

import config
import credentials
import response_shapes
from replicon_client import RepliconClient, RepliconAPIError

_LOGIN_SCOPES = ["User.Read"]

# MSAL's confidential-client auth-code flow needs its own small server-side
# state (code_verifier/nonce) round-tripped between /link and /link/callback.
# Keyed by the flow's own `state` value; expires quickly, so an in-memory
# dict is fine for a single-process deployment.
_pending_flows: dict[str, dict] = {}

# Bridges /link/search -> /link/submit: holds the just-submitted Replicon
# bearer token (already validated by find_users succeeding) so the
# confirm/select step doesn't need to round-trip the token through the
# browser a second time. Keyed by the same link_code as
# credentials.link_sessions. In-memory only, never persisted, cleared as
# soon as /link/submit consumes it.
_pending_credentials: dict[str, dict] = {}


def _msal_app():
    import msal

    tenant_id = os.environ["ENTRA_TENANT_ID"]
    client_id = os.environ["ENTRA_CLIENT_ID"]
    client_secret = os.environ["ENTRA_CLIENT_SECRET"]
    return msal.ConfidentialClientApplication(
        client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )


def _redirect_uri() -> str:
    public_url = os.environ["PUBLIC_URL"].rstrip("/")
    return f"{public_url}/link/callback"


_PAGE_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 30rem; margin: 3rem auto; padding: 0 1rem; color: #222; }
  .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 12px; padding: 1.5rem 1.75rem; }
  h1 { font-size: 20px; margin: 0 0 12px; }
  .lede { font-size: 14px; color: #555; line-height: 1.6; margin: 0 0 20px; }
  .warn { background: #fff8e1; border-radius: 6px; padding: 10px 12px; margin: 0 0 20px; }
  .warn p { font-size: 13px; color: #8a6d00; margin: 0; line-height: 1.5; }
  .error { background: #fdecea; border-radius: 6px; padding: 10px 14px; margin: 0 0 20px; }
  .error p { color: #b00020; font-size: 14px; margin: 0; }
  ol { margin: 0 0 24px; padding-left: 20px; }
  li { font-size: 14px; line-height: 1.6; margin-bottom: 12px; }
  li:last-child { margin-bottom: 0; }
  code { font-family: monospace; background: #f4f4f4; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
  form { border-top: 1px solid #eee; padding-top: 20px; }
  label { display: block; font-size: 13px; color: #555; margin-bottom: 6px; }
  input[type=text], input[type=password] { width: 100%; box-sizing: border-box; padding: 8px 10px;
         border: 1px solid #ccc; border-radius: 6px; font-size: 14px; margin-bottom: 14px; }
  button { width: 100%; padding: 10px; border: none; border-radius: 6px; background: #1a1a1a;
           color: #fff; font-size: 14px; font-weight: 500; cursor: pointer; }
  .radio-row { font-size: 14px; margin-bottom: 10px; }
</style>
"""


def _page(body: str) -> str:
    return f'<html><head>{_PAGE_STYLE}</head><body><div class="card">{body}</div></body></html>'


def _error_page(message: str) -> str:
    return _page(f'<h1>Something went wrong</h1><p class="lede">{html.escape(message)}</p>')


def _credentials_form(link_code: str, error: str = "") -> str:
    error_html = f'<div class="error"><p>{html.escape(error)}</p></div>' if error else ""
    return _page(f"""
        <h1>Link your Replicon account</h1>
        <p class="lede">Generate a personal access token from Replicon, then paste it in below.</p>
        {error_html}
        <div class="warn"><p>Use the <code>eu1</code> domain in the steps below, not <code>sb1</code>.</p></div>
        <ol>
            <li>Open a new tab and make sure you're logged into
                <a href="https://eu1.replicon.com" target="_blank" rel="noopener">eu1.replicon.com</a>.</li>
            <li>In another tab, open Replicon's
                <a href="https://eu1.replicon.com/services/docs/security.html" target="_blank" rel="noopener">Security API page</a>.</li>
            <li>Scroll to <strong>Creating and revoking access tokens</strong> and expand <code>/CreateAccessToken2</code>.</li>
            <li>Click <strong>Try it out</strong>.</li>
            <li>Fill in <code>loginName</code> with your email. <code>description</code> and <code>unitOfWorkId</code> are optional.</li>
            <li>Click <strong>Execute</strong>.</li>
            <li>Scroll to the <strong>Response body</strong> and copy the <code>token</code> value.</li>
        </ol>
        <form method="post" action="/link/search">
            <input type="hidden" name="link_code" value="{html.escape(link_code)}">
            <label>Replicon API token</label>
            <input name="bearer_token" type="password" required placeholder="Paste the token value here">
            <label>Your name in Replicon</label>
            <input name="display_name" type="text" required placeholder="e.g. Cheah, Chee Loung">
            <button type="submit">Find my account</button>
        </form>
    """)


async def _link_start(request: Request):
    flow = _msal_app().initiate_auth_code_flow(
        _LOGIN_SCOPES, redirect_uri=_redirect_uri()
    )
    _pending_flows[flow["state"]] = flow
    return RedirectResponse(flow["auth_uri"])


async def _link_callback(request: Request):
    state = request.query_params.get("state")
    flow = _pending_flows.pop(state, None) if state else None
    if flow is None:
        return HTMLResponse(
            _error_page("Login session expired or invalid — go back and try /link again."),
            status_code=400,
        )

    result = _msal_app().acquire_token_by_auth_code_flow(
        flow, dict(request.query_params)
    )
    if "error" in result:
        return HTMLResponse(
            _error_page(f"Login failed: {result.get('error_description', result['error'])}"),
            status_code=400,
        )

    subject = result.get("id_token_claims", {}).get("oid")
    if not subject:
        return HTMLResponse(
            _error_page("Login succeeded but no user id was returned."), status_code=400
        )

    link_code = credentials.create_link_session(subject)
    return HTMLResponse(_credentials_form(link_code))


async def _link_search(request: Request):
    form = await request.form()
    link_code = form.get("link_code") or ""
    bearer_token = (form.get("bearer_token") or "").strip()
    display_name = (form.get("display_name") or "").strip()

    if credentials.peek_link_session(link_code) is None:
        return HTMLResponse(
            _error_page("This link session expired — go back and try /link again."), status_code=400
        )
    if not bearer_token or not display_name:
        return HTMLResponse(_credentials_form(link_code, "All fields are required."))

    try:
        client = RepliconClient(config.get_base_url(), bearer_token=bearer_token)
        raw = client.find_users(name_search=display_name)
    except RepliconAPIError as e:
        return HTMLResponse(
            _credentials_form(link_code, f"Could not sign in to Replicon: {e}")
        )

    matches = response_shapes.shape_user_list(raw)
    if not matches:
        return HTMLResponse(
            _credentials_form(link_code, f"No Replicon user found matching '{display_name}'. Try a shorter name.")
        )

    _pending_credentials[link_code] = {"bearer_token": bearer_token}

    options = "\n".join(
        f'<div class="radio-row"><label><input type="radio" name="user_uri" value="{html.escape(m["uri"])}" '
        f'{"checked" if i == 0 else ""}> {html.escape(m["name"])}</label></div>'
        for i, m in enumerate(matches)
    )
    return HTMLResponse(_page(f"""
        <h1>Confirm your Replicon account</h1>
        <p class="lede">Found {len(matches)} match(es) for '{html.escape(display_name)}' — pick yours:</p>
        <form method="post" action="/link/submit">
            <input type="hidden" name="link_code" value="{html.escape(link_code)}">
            {options}
            <button type="submit">Link account</button>
        </form>
    """))


async def _link_submit(request: Request):
    form = await request.form()
    link_code = form.get("link_code") or ""
    user_uri = (form.get("user_uri") or "").strip()

    pending = _pending_credentials.pop(link_code, None)
    subject = credentials.consume_link_session(link_code) if link_code else None
    if subject is None or pending is None:
        return HTMLResponse(
            _error_page("This link session expired — go back and try /link again."), status_code=400
        )
    if not user_uri:
        return HTMLResponse(_error_page("No account selected."), status_code=400)

    credentials.set_replicon_credentials(
        subject, pending["bearer_token"], user_uri
    )
    return HTMLResponse(_page(
        '<h1>Linked</h1><p class="lede">Your Replicon account is linked. '
        "You can close this tab and use the replicon-mcp connector in Claude.</p>"
    ))


def register(mcp) -> None:
    """Wire the /link onboarding routes onto a FastMCP instance running in HTTP mode."""
    mcp.custom_route("/link", methods=["GET"])(_link_start)
    mcp.custom_route("/link/callback", methods=["GET"])(_link_callback)
    mcp.custom_route("/link/search", methods=["POST"])(_link_search)
    mcp.custom_route("/link/submit", methods=["POST"])(_link_submit)
