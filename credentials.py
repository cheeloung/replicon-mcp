"""Per-user Replicon credential storage, keyed by Entra ID subject (oid).

Used only when the server runs in streamable-http mode with Entra auth. Each
user links their own Replicon personal API (bearer) token once via the
/link web flow (see onboarding.py), which also resolves their Replicon user
URI via a name search (Replicon has no "who am I" endpoint). The encrypted
result is looked up per MCP request by _resolve_caller() in server.py.

Bearer token, not username/password: Basic Auth fails outright (401, no
useful error) for any Replicon account with 2-step verification enabled —
confirmed live against this tenant. A personal API token (generated per
https://sb1.replicon.com/services/docs/security.html) sidesteps MFA
entirely, which is also why the original local single-user .env for this
project used REPLICON_BEARER_TOKEN rather than Basic Auth.

REPLICON_BASE_URL is NOT stored here — it's the same for the whole org (one
Kranz Wolfe tenant) and stays as a shared server-level env var (see
config.py). REPLICON_COMPANY_KEY is no longer needed for remote/hosted mode
at all now that /link only collects a bearer token — it's still used by
config.py for local stdio Basic Auth installs.
"""

import os
import sqlite3
import time
import uuid
from contextlib import closing
from cryptography.fernet import Fernet, InvalidToken

_LINK_SESSION_TTL_SECONDS = 15 * 60


def _db_path() -> str:
    return os.getenv("CREDENTIAL_DB_PATH", "replicon-mcp-credentials.db")


def _fernet() -> Fernet:
    key = os.getenv("CREDENTIAL_STORE_KEY")
    if not key:
        raise RuntimeError(
            "CREDENTIAL_STORE_KEY environment variable must be set to a Fernet key "
            '(generate one with `python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`).'
        )
    return Fernet(key.encode())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replicon_credentials (
            subject TEXT PRIMARY KEY,
            encrypted_token BLOB NOT NULL,
            user_uri TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS link_sessions (
            code TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    return conn


def get_replicon_credentials(subject: str) -> tuple[str, str] | None:
    """Return (bearer_token, user_uri) for a linked Entra subject, or None if unlinked."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT encrypted_token, user_uri FROM replicon_credentials WHERE subject = ?",
            (subject,),
        ).fetchone()
    if row is None:
        return None
    encrypted_token, user_uri = row
    try:
        token = _fernet().decrypt(bytes(encrypted_token)).decode()
    except InvalidToken:
        return None
    return token, user_uri


def set_replicon_credentials(subject: str, bearer_token: str, user_uri: str) -> None:
    """Store (or overwrite) a user's linked Replicon bearer token + resolved user URI,
    encrypted at rest."""
    encrypted_token = _fernet().encrypt(bearer_token.encode())
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO replicon_credentials (subject, encrypted_token, user_uri, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                encrypted_token = excluded.encrypted_token,
                user_uri = excluded.user_uri,
                updated_at = excluded.updated_at
            """,
            (subject, encrypted_token, user_uri, time.time()),
        )
        conn.commit()


def delete_replicon_credentials(subject: str) -> None:
    """Revoke a user's linked Replicon credentials (e.g. on request or offboarding)."""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM replicon_credentials WHERE subject = ?", (subject,))
        conn.commit()


def create_link_session(subject: str) -> str:
    """Create a short-lived one-time code proving `subject` completed Entra login,
    so the /link/search and /link/submit form POSTs don't need a cookie session."""
    code = uuid.uuid4().hex
    with closing(_connect()) as conn:
        conn.execute(
            "DELETE FROM link_sessions WHERE created_at < ?",
            (time.time() - _LINK_SESSION_TTL_SECONDS,),
        )
        conn.execute(
            "INSERT INTO link_sessions (code, subject, created_at) VALUES (?, ?, ?)",
            (code, subject, time.time()),
        )
        conn.commit()
    return code


def peek_link_session(code: str) -> str | None:
    """Check a link-session code is still valid WITHOUT consuming it — used
    between /link/callback and /link/search->/link/submit, since the same
    code is presented across two POSTs (search, then submit) before the
    identity is finally consumed."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT subject, created_at FROM link_sessions WHERE code = ?", (code,)
        ).fetchone()
    if row is None:
        return None
    subject, created_at = row
    if time.time() - created_at > _LINK_SESSION_TTL_SECONDS:
        return None
    return subject


def consume_link_session(code: str) -> str | None:
    """Redeem a one-time link-session code, returning the Entra subject it was issued
    for, or None if the code is missing/expired/already used."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT subject, created_at FROM link_sessions WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM link_sessions WHERE code = ?", (code,))
        conn.commit()
    subject, created_at = row
    if time.time() - created_at > _LINK_SESSION_TTL_SECONDS:
        return None
    return subject
