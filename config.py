"""
config.py — Load and validate environment configuration.
Reads from .env file in the same directory as this script.
"""

import os
import base64
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (same dir as this file)
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)


def get_base_url() -> str:
    url = os.getenv("REPLICON_BASE_URL", "").strip().rstrip("/")
    if not url:
        raise ValueError("REPLICON_BASE_URL is not set in .env")
    return url


def get_company_key() -> str:
    """
    Returns the shared Replicon company key (tenant identifier) — the same
    for every teammate on this tenant, unlike a personal username/password.
    """
    company = os.getenv("REPLICON_COMPANY_KEY", "").strip()
    if not company:
        raise ValueError("REPLICON_COMPANY_KEY is not set in .env")
    return company


def basic_auth_header(username: str, password: str, company: str) -> dict:
    """Build a Replicon HTTP Basic Auth header from company\\username:password."""
    credentials = f"{company}\\{username}:{password}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def get_auth_headers() -> dict:
    """
    Returns Authorization header dict for the single shared credential
    configured in this process's .env (stdio/local mode only).
    Bearer token takes precedence; falls back to HTTP Basic Auth.
    """
    bearer = os.getenv("REPLICON_BEARER_TOKEN", "").strip()
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}

    username = os.getenv("REPLICON_USERNAME", "").strip()
    password = os.getenv("REPLICON_PASSWORD", "").strip()

    if username and password:
        return basic_auth_header(username, password, get_company_key())

    raise ValueError(
        "No auth configured. Set REPLICON_BEARER_TOKEN, or "
        "REPLICON_COMPANY_KEY + REPLICON_USERNAME + REPLICON_PASSWORD, in .env"
    )


# Tenant-specific extension field URI for the "Reference Tuleap" free-text column.
# Discovered via probe on 2026-06-26 against tenant 7c3ab9f5ed424106aa53ee7b6ef2b1c7.
# Override via REPLICON_TULEAP_FIELD_URI in .env if your tenant differs.
TULEAP_FIELD_URI: str = os.getenv(
    "REPLICON_TULEAP_FIELD_URI",
    "urn:replicon-tenant:7c3ab9f5ed424106aa53ee7b6ef2b1c7"
    ":object-extension-tag-definition:88c8d7db-6f71-4dde-8465-3420660e4fd6",
)


def get_user_uri() -> str:
    """
    Returns the authenticated user's Replicon URI.
    Required for tools that operate on 'my' timesheet or filter pending
    approvals by the current approver.

    To find your URI: run `python find_my_user_uri.py "Your Name"` (only needs
    REPLICON_BASE_URL and your auth already set in .env — see README.md).
    Format: urn:replicon-tenant:{tenant_id}:user:{id}
    """
    uri = os.getenv("REPLICON_USER_URI", "").strip()
    if not uri:
        raise ValueError(
            "REPLICON_USER_URI is not set in .env. "
            "Set it to your Replicon user URI, e.g. "
            "urn:replicon-tenant:abc123:user:42"
        )
    return uri


def validate():
    """Call on startup to catch misconfigurations early."""
    get_base_url()
    get_auth_headers()
    # User URI is validated lazily (some tools don't need it)
