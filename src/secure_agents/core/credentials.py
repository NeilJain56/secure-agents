"""Secure credential storage.

Credentials are NEVER stored in config files. This module provides a layered
lookup that checks (in order):

    1. macOS Keychain (via the `keyring` library)
    2. Environment variables
    3. OAuth2 token file (for Gmail and other OAuth providers)

For Gmail specifically, plain passwords won't work - Google requires either:
    - An App Password (with 2FA enabled on your Google account)
    - OAuth2 (recommended - this module handles the token flow)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

logger = structlog.get_logger()

SERVICE_NAME = "secure-agents"
TOKEN_DIR = Path("~/.secure-agents/tokens").expanduser()


def get_credential(key: str) -> str | None:
    """Retrieve a credential by key. Tries keychain, then env vars.

    Args:
        key: Credential name, e.g. "email_password", "anthropic_api_key"

    Returns:
        The credential value, or None if not found.
    """
    # 1. Try macOS Keychain
    value = _get_from_keychain(key)
    if value:
        return value

    # 2. Try environment variable (uppercase)
    env_key = key.upper()
    value = os.environ.get(env_key)
    if value:
        return value

    return None


def store_credential(key: str, value: str) -> bool:
    """Store a credential in the macOS Keychain.

    Returns True if stored successfully, False otherwise.
    """
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, key, value)
        logger.info("credentials.stored", key=key, backend="keychain")
        return True
    except Exception as e:
        logger.warning("credentials.store_failed", key=key, error=str(e))
        return False


def delete_credential(key: str) -> bool:
    """Remove a credential from the macOS Keychain."""
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, key)
        return True
    except Exception:
        return False


def _get_from_keychain(key: str) -> str | None:
    """Try to read from macOS Keychain."""
    try:
        import keyring
        value = keyring.get_password(SERVICE_NAME, key)
        if value:
            logger.debug("credentials.keychain_hit", key=key)
        return value
    except ImportError:
        return None
    except Exception:
        return None


# --- OAuth2 support for Gmail ---

def get_oauth2_token(account: str) -> str | None:
    """Get a cached OAuth2 access token for an email account.

    Returns None if no token exists or it can't be refreshed.
    Use `run_oauth2_flow()` to create a token interactively.
    """
    token_path = TOKEN_DIR / f"{_safe_filename(account)}.json"
    if not token_path.exists():
        return None

    try:
        token_data = json.loads(token_path.read_text())
        # Try to refresh if we have a refresh token
        if "refresh_token" in token_data and "client_id" in token_data:
            return _refresh_oauth2_token(token_data, token_path)
        return token_data.get("access_token")
    except Exception as e:
        logger.error("credentials.oauth2_read_error", error=str(e))
        return None


def run_oauth2_flow(client_secrets_path: str, account: str) -> bool:
    """Run the OAuth2 authorization flow for Gmail.

    This opens a browser for the user to authorize access. The resulting
    token is stored locally in ~/.secure-agents/tokens/.

    Args:
        client_secrets_path: Path to Google OAuth2 client_secrets.json
        account: Email address (used as filename for token storage)

    Returns:
        True if the flow completed successfully.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        logger.error("credentials.oauth2_missing_dep",
                      msg="Install with: pip install google-auth-oauthlib")
        return False

    SCOPES = [
        "https://mail.google.com/",  # IMAP access (required for IMAP auth)
    ]

    try:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
        creds = flow.run_local_server(port=0)

        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = TOKEN_DIR / f"{_safe_filename(account)}.json"
        token_data = {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
        token_path.write_text(json.dumps(token_data))
        # Restrict file permissions
        token_path.chmod(0o600)

        logger.info("credentials.oauth2_success", account=account)
        return True
    except Exception as e:
        logger.error("credentials.oauth2_flow_error", error=str(e))
        return False


def _refresh_oauth2_token(token_data: dict, token_path: Path) -> str | None:
    """Refresh an OAuth2 access token using the refresh token."""
    try:
        import httpx
        response = httpx.post(
            token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "client_id": token_data["client_id"],
                "client_secret": token_data["client_secret"],
                "refresh_token": token_data["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        new_token = response.json()["access_token"]

        # Update stored token
        token_data["access_token"] = new_token
        token_path.write_text(json.dumps(token_data))
        token_path.chmod(0o600)

        return new_token
    except Exception as e:
        logger.warning("credentials.oauth2_refresh_failed", error=str(e))
        return token_data.get("access_token")


def _safe_filename(s: str) -> str:
    """Convert a string to a safe filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)
