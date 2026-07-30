"""The one-time '/link' web flow: a user signs in with Entra ID, then enters
their personal Replicon username/password so future MCP calls made under
their Entra identity can use their own Replicon credentials.

Unlike tuleap-mcp's onboarding (a single URL + API key form), Replicon has no
"who am I" endpoint — the same gap find_my_user_uri.py works around locally
by searching for yourself by name. So this flow has an extra step: after
collecting credentials, it searches Replicon for the display name the user
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
# username/password (already validated by find_users succeeding) so the
# confirm/select step doesn't need to round-trip a plaintext password
# through the browser a second time. Keyed by the same link_code as
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


def _credentials_form(link_code: str, error: str = "") -> str:
    error_html = f'<p style="color:#b00">{html.escape(error)}</p>' if error else ""
    return f"""
        <html><body style="font-family: sans-serif; max-width: 32rem; margin: 3rem auto;">
        <h2>Link your Replicon account</h2>
        <p>Signed in. Enter your normal Replicon login below, plus your name as it
        appears in Replicon (used to find your account — Replicon has no direct
        "who am I" lookup). Your password is stored encrypted and only used for
        MCP calls made under your identity.</p>
        {error_html}
        <form method="post" action="/link/search">
            <input type="hidden" name="link_code" value="{html.escape(link_code)}">
            <p><label>Replicon username (your login email)<br>
                <input name="username" type="text" required style="width:100%"></label></p>
            <p><label>Replicon password<br>
                <input name="password" type="password" required style="width:100%"></label></p>
            <p><label>Your name as it appears in Replicon<br>
                <input name="display_name" type="text" required style="width:100%"
                       placeholder="e.g. Cheah, Chee Loung"></label></p>
            <button type="submit">Find my account</button>
        </form>
        </body></html>
    """


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
            "Login session expired or invalid — go back and try /link again.",
            status_code=400,
        )

    result = _msal_app().acquire_token_by_auth_code_flow(
        flow, dict(request.query_params)
    )
    if "error" in result:
        return HTMLResponse(
            f"Login failed: {result.get('error_description', result['error'])}",
            status_code=400,
        )

    subject = result.get("id_token_claims", {}).get("oid")
    if not subject:
        return HTMLResponse(
            "Login succeeded but no user id was returned.", status_code=400
        )

    link_code = credentials.create_link_session(subject)
    return HTMLResponse(_credentials_form(link_code))


async def _link_search(request: Request):
    form = await request.form()
    link_code = form.get("link_code") or ""
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    display_name = (form.get("display_name") or "").strip()

    if credentials.peek_link_session(link_code) is None:
        return HTMLResponse(
            "This link session expired — go back and try /link again.", status_code=400
        )
    if not username or not password or not display_name:
        return HTMLResponse(_credentials_form(link_code, "All fields are required."))

    try:
        client = RepliconClient(config.get_base_url(), username=username, password=password)
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

    _pending_credentials[link_code] = {"username": username, "password": password}

    options = "\n".join(
        f'<p><label><input type="radio" name="user_uri" value="{html.escape(m["uri"])}" '
        f'{"checked" if i == 0 else ""}> {html.escape(m["name"])}</label></p>'
        for i, m in enumerate(matches)
    )
    return HTMLResponse(f"""
        <html><body style="font-family: sans-serif; max-width: 32rem; margin: 3rem auto;">
        <h2>Confirm your Replicon account</h2>
        <p>Found {len(matches)} match(es) for '{html.escape(display_name)}' — pick yours:</p>
        <form method="post" action="/link/submit">
            <input type="hidden" name="link_code" value="{html.escape(link_code)}">
            {options}
            <button type="submit">Link account</button>
        </form>
        </body></html>
    """)


async def _link_submit(request: Request):
    form = await request.form()
    link_code = form.get("link_code") or ""
    user_uri = (form.get("user_uri") or "").strip()

    pending = _pending_credentials.pop(link_code, None)
    subject = credentials.consume_link_session(link_code) if link_code else None
    if subject is None or pending is None:
        return HTMLResponse(
            "This link session expired — go back and try /link again.", status_code=400
        )
    if not user_uri:
        return HTMLResponse("No account selected.", status_code=400)

    credentials.set_replicon_credentials(
        subject, pending["username"], pending["password"], user_uri
    )
    return HTMLResponse(
        "<html><body style='font-family: sans-serif; max-width: 32rem; margin: 3rem auto;'>"
        "<h2>Linked</h2><p>Your Replicon account is linked. You can close this tab and use "
        "the replicon-mcp connector in Claude.</p></body></html>"
    )


def register(mcp) -> None:
    """Wire the /link onboarding routes onto a FastMCP instance running in HTTP mode."""
    mcp.custom_route("/link", methods=["GET"])(_link_start)
    mcp.custom_route("/link/callback", methods=["GET"])(_link_callback)
    mcp.custom_route("/link/search", methods=["POST"])(_link_search)
    mcp.custom_route("/link/submit", methods=["POST"])(_link_submit)
