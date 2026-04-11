"""Secure credential storage.

Credentials are NEVER stored in config files. This module exposes the
small API the rest of the framework uses (``get_credential``,
``store_credential``, ``delete_credential``) and delegates to a
pluggable backend defined in ``credential_backends.py``.

Backend selection happens at startup via ``configure_credentials()``,
which is called from the CLI and dashboard bootstrap after
``load_config()`` runs.  Until that happens, an ``"auto"`` backend is
used as a fallback so that scripts that import this module without
booting the full framework still work.

Lookup order on every read:

    1. The configured primary backend (Keychain or encrypted file).
    2. Environment variables (uppercased), as a per-secret override.
    3. None.

This means a user can drop a one-off ``EMAIL_PASSWORD=... secure-agents
start ...`` invocation without re-running setup, while the persistent
store remains the source of truth.

For Gmail specifically, plain passwords won't work — Google requires
either an App Password (with 2FA enabled) or OAuth2.  OAuth2 tokens are
stored in ``~/.secure-agents/tokens/<account>.json`` with ``0600``
permissions; the OAuth2 ``client_secret`` is stored in the configured
credential backend, never on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from secure_agents.core.credential_backends import (
    DEFAULT_STORE_PATH,
    MASTER_KEY_ENV,
    CredentialBackend,
    EncryptedFileBackend,
    EnvBackend,
    KeychainBackend,
    MasterKeyError,
    resolve_backend,
)

logger = structlog.get_logger()

SERVICE_NAME = "secure-agents"
TOKEN_DIR = Path("~/.secure-agents/tokens").expanduser()

# Re-exports so existing imports of these names still work.
__all__ = [
    "MASTER_KEY_ENV",
    "MasterKeyError",
    "configure_credentials",
    "get_active_backend",
    "get_credential",
    "store_credential",
    "delete_credential",
    "get_oauth2_token",
    "run_oauth2_flow",
]

_active_backend: CredentialBackend | None = None
_env_backend = EnvBackend()


def configure_credentials(
    backend: str = "auto",
    store_path: str | Path | None = None,
    *,
    interactive: bool = True,
) -> CredentialBackend:
    """Select the primary credential backend for this process.

    Called from ``cli.py`` and ``ui/server.py`` after ``load_config()``.
    Returns the resolved backend so callers can log which one is active.
    """
    global _active_backend
    _active_backend = resolve_backend(backend, store_path, interactive=interactive)
    logger.info(
        "credentials.backend_configured",
        backend=_active_backend.name,
        store_path=str(store_path) if store_path else "",
    )
    return _active_backend


def get_active_backend() -> CredentialBackend:
    """Return the configured backend, lazily initializing one if needed."""
    global _active_backend
    if _active_backend is None:
        _active_backend = resolve_backend("auto")
    return _active_backend


def get_credential(key: str) -> str | None:
    """Retrieve a credential by key.

    Args:
        key: Credential name, e.g. ``"email_password"``.

    Returns:
        The credential value, or ``None`` if not found in either the
        configured backend or the environment.
    """
    backend = get_active_backend()
    value = backend.get(key)
    if value:
        return value
    # Environment variable override / fallback.
    return _env_backend.get(key)


def store_credential(key: str, value: str) -> bool:
    """Store a credential in the active backend.  Returns ``True`` on success."""
    return get_active_backend().set(key, value)


def delete_credential(key: str) -> bool:
    """Remove a credential from the active backend."""
    return get_active_backend().delete(key)


# --- OAuth2 support for Gmail ---


def get_oauth2_token(account: str) -> str | None:
    """Get a cached OAuth2 access token for an email account.

    Returns ``None`` if no token exists or it can't be refreshed.  Use
    ``run_oauth2_flow()`` to create a token interactively.
    """
    token_path = TOKEN_DIR / f"{_safe_filename(account)}.json"
    if not token_path.exists():
        return None

    try:
        token_data = json.loads(token_path.read_text())
        # Try to refresh if we have a refresh token.
        if "refresh_token" in token_data:
            # client_secret is stored in the credential backend, not on disk.
            client_secret = get_credential("oauth2_client_secret")
            client_id = token_data.get("client_id", "")
            if client_secret and client_id:
                return _refresh_oauth2_token(token_data, token_path, client_id, client_secret)
        return token_data.get("access_token")
    except Exception as e:
        logger.error("credentials.oauth2_read_error", error=str(e))
        return None


def run_oauth2_flow(client_secrets_path: str, account: str) -> bool:
    """Run the OAuth2 authorization flow for Gmail.

    Opens a browser for the user to authorize access.  The resulting
    token is stored locally in ``~/.secure-agents/tokens/`` (access +
    refresh tokens only — the ``client_secret`` is stored in the
    configured credential backend, never on disk).

    Args:
        client_secrets_path: Path to Google OAuth2 ``client_secrets.json``.
        account: Email address (used as the token filename).

    Returns:
        ``True`` if the flow completed successfully.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        logger.error(
            "credentials.oauth2_missing_dep",
            msg="Install with: pip install google-auth-oauthlib",
        )
        return False

    SCOPES = [
        "https://mail.google.com/",  # IMAP access (required for IMAP auth)
    ]

    try:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
        creds = flow.run_local_server(port=0)

        # Store client_secret in the configured backend — NEVER on disk.
        if creds.client_secret:
            store_credential("oauth2_client_secret", creds.client_secret)

        # Store ONLY tokens on disk (not the client_secret).
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = TOKEN_DIR / f"{_safe_filename(account)}.json"
        token_data = {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            # client_secret is NOT stored here — it's in the credential backend.
        }
        token_path.write_text(json.dumps(token_data))
        token_path.chmod(0o600)

        logger.info("credentials.oauth2_success", account=account)
        return True
    except Exception as e:
        logger.error("credentials.oauth2_flow_error", error=str(e))
        return False


def _refresh_oauth2_token(
    token_data: dict, token_path: Path, client_id: str, client_secret: str
) -> str | None:
    """Refresh an OAuth2 access token using the refresh token."""
    try:
        import httpx
        response = httpx.post(
            token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": token_data["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=10.0,  # Hard timeout to prevent hanging
            verify=True,   # Explicitly enforce TLS verification
        )
        response.raise_for_status()

        new_data = response.json()
        if "access_token" not in new_data:
            logger.warning("credentials.oauth2_refresh_no_token")
            return token_data.get("access_token")

        new_token = new_data["access_token"]

        # Update stored token (still no client_secret on disk).
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
