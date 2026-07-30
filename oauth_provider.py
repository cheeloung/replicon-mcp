"""MCP OAuth Authorization Server that proxies Kranz Wolfe's Entra ID as the
upstream identity provider.

claude.ai's connector implementation expects the MCP server itself to be a
full OAuth Authorization Server (hitting /authorize and /token on this same
domain) rather than delegating to an external AS via protected-resource
metadata, so this module implements that contract while doing the actual
login against Entra underneath — the standard "MCP server as OAuth proxy
for a 3rd-party IdP" pattern described in the SDK's own provider docstring.

PKCE verification, redirect_uri matching, and auth-code expiry are all
enforced by the SDK's own token endpoint handler before any of these
methods are called — this module only needs to do the Entra proxy leg and
bookkeeping. Everything here is ephemeral, in-memory, per-process state
(unlike the durable per-user Replicon credentials in credentials.py) — a
restart just means connected clients need to reauthenticate.

Uses a SEPARATE Azure App Registration from tuleap-mcp's — see
docs/remote-hosting.md.
"""

import logging
import os
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    construct_redirect_uri,
)
from pydantic import AnyUrl

logger = logging.getLogger(__name__)

_LOGIN_SCOPES = ["User.Read"]
_AUTH_CODE_TTL_SECONDS = 5 * 60
_ACCESS_TOKEN_TTL_SECONDS = 60 * 60


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


def _entra_redirect_uri() -> str:
    public_url = os.environ["PUBLIC_URL"].rstrip("/")
    return f"{public_url}/oauth/entra/callback"


class EntraProxyOAuthProvider(OAuthAuthorizationServerProvider):
    """Presents as a full OAuth Authorization Server to MCP clients (e.g.
    claude.ai), but delegates the actual login to Entra ID and mints its own
    opaque tokens scoped to the authenticated user's Entra subject (oid)."""

    def __init__(self, client_id: str, redirect_uris: list[str]):
        self._client_id = client_id
        self._redirect_uris = [AnyUrl(u) for u in redirect_uris]
        self._pending_flows: dict[str, dict] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id != self._client_id:
            return None
        return OAuthClientInformationFull(
            client_id=self._client_id,
            client_secret=None,
            redirect_uris=self._redirect_uris,
            # RFC 7591 enum value meaning "no client secret" (public/PKCE-only
            # client) — not a credential, despite bandit's heuristic below.
            token_endpoint_auth_method="none",  # nosec B106
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("Dynamic client registration is not supported")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        flow = _msal_app().initiate_auth_code_flow(
            _LOGIN_SCOPES, redirect_uri=_entra_redirect_uri()
        )
        self._pending_flows[flow["state"]] = {
            "flow": flow,
            "params": params,
            "client_id": client.client_id,
        }
        logger.info(
            "authorize(): stored pending Entra flow state=%s (now tracking %d pending)",
            flow["state"],
            len(self._pending_flows),
        )
        return flow["auth_uri"]

    def complete_entra_login(self, query_params: dict) -> str | None:
        """Called by the /oauth/entra/callback route once Entra redirects back
        here. Returns the URL to send the browser to next (back to the
        original MCP client, e.g. claude.ai), or None if the state doesn't
        match a pending flow or the Entra exchange failed."""
        state = query_params.get("state")
        pending = self._pending_flows.pop(state, None) if state else None
        if pending is None:
            logger.warning(
                "complete_entra_login(): no pending flow for state=%s (tracking %d pending: %s)",
                state,
                len(self._pending_flows),
                list(self._pending_flows.keys()),
            )
            return None

        result = _msal_app().acquire_token_by_auth_code_flow(
            pending["flow"], query_params
        )
        if "error" in result:
            logger.warning(
                "complete_entra_login(): MSAL token exchange failed: %s — %s",
                result.get("error"),
                result.get("error_description"),
            )
            return None
        subject = result.get("id_token_claims", {}).get("oid")
        if not subject:
            logger.warning(
                "complete_entra_login(): MSAL result had no id_token_claims.oid: %s",
                result,
            )
            return None

        params: AuthorizationParams = pending["params"]
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=subject,
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            client.client_id, authorization_code.scopes, authorization_code.subject
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue_tokens(
            client.client_id, scopes or refresh_token.scopes, refresh_token.subject
        )

    def _issue_tokens(
        self, client_id: str, scopes: list[str], subject: str | None
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            subject=subject,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token, client_id=client_id, scopes=scopes, subject=subject
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self._access_tokens.get(token)
        if access_token is None:
            return None
        if (
            access_token.expires_at is not None
            and access_token.expires_at < time.time()
        ):
            del self._access_tokens[token]
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)


def provider_from_env() -> EntraProxyOAuthProvider:
    client_id = os.environ["ENTRA_CLIENT_ID"]
    redirect_uris = [
        u.strip()
        for u in os.environ["MCP_CLIENT_REDIRECT_URIS"].split(",")
        if u.strip()
    ]
    return EntraProxyOAuthProvider(client_id, redirect_uris)
