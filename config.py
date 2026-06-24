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


def get_auth_headers() -> dict:
    """
    Returns Authorization header dict.
    Bearer token takes precedence; falls back to HTTP Basic Auth.
    """
    bearer = os.getenv("REPLICON_BEARER_TOKEN", "").strip()
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}

    username = os.getenv("REPLICON_USERNAME", "").strip()
    password = os.getenv("REPLICON_PASSWORD", "").strip()
    company = os.getenv("REPLICON_COMPANY_KEY", "").strip()

    if username and password:
        if not company:
            raise ValueError(
                "REPLICON_COMPANY_KEY is required when using Basic Auth "
                "(REPLICON_USERNAME + REPLICON_PASSWORD)."
            )
        # Replicon basic auth format: company\username:password
        credentials = f"{company}\\{username}:{password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    raise ValueError(
        "No auth configured. Set REPLICON_BEARER_TOKEN, or "
        "REPLICON_COMPANY_KEY + REPLICON_USERNAME + REPLICON_PASSWORD, in .env"
    )


def validate():
    """Call on startup to catch misconfigurations early."""
    get_base_url()
    get_auth_headers()
