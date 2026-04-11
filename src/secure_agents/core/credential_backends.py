"""Pluggable credential storage backends.

The framework supports several places where a credential can live, so the
same code can run on a Mac developer machine, a Linux VM, or a hardened
container without changing how tools fetch their secrets.

Backends:

* ``KeychainBackend``     -- macOS Keychain via the ``keyring`` library.
                             Convenient on a developer Mac, requires the
                             user to be logged in.
* ``EncryptedFileBackend`` -- AES-256-GCM encrypted file under
                             ``~/.secure-agents/credentials.enc``.  Works
                             on any OS, designed for VMs and headless
                             servers.  The encryption key is derived from
                             a master passphrase via scrypt; the
                             passphrase comes from the
                             ``SECURE_AGENTS_MASTER_KEY`` env var or an
                             interactive prompt cached for the lifetime
                             of the process.
* ``EnvBackend``           -- read-only fallback that resolves
                             ``KEY`` -> ``os.environ["KEY".upper()]``.
                             Useful for CI / unattended deployments and
                             always consulted as a last resort.

The factory function ``resolve_backend(name, store_path)`` returns the
appropriate backend for a given configuration name (``"keychain"``,
``"encrypted_file"``, or ``"auto"``).  ``"auto"`` picks the most secure
backend that is actually usable on the current host.

All write operations on the encrypted store fail closed: if the master
key cannot be obtained, the call returns ``False`` and the operation is
aborted rather than silently storing the credential in plaintext.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import platform
import secrets
import sys
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

SERVICE_NAME = "secure-agents"
DEFAULT_STORE_PATH = Path("~/.secure-agents/credentials.enc").expanduser()
MASTER_KEY_ENV = "SECURE_AGENTS_MASTER_KEY"
MIN_PASSPHRASE_LEN = 8

# scrypt parameters: ~32 MiB memory, ~100 ms on a modern CPU.  These are
# strong enough to make offline brute force expensive while keeping the
# one-time startup cost reasonable.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32  # AES-256
_NONCE_LEN = 12
_SALT_LEN = 16
_FILE_VERSION = 1


# ── Backend interface ────────────────────────────────────────────────────────


class CredentialBackend(ABC):
    """Abstract storage backend for secrets."""

    name: str = ""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the credential for ``key`` or ``None`` if absent."""

    @abstractmethod
    def set(self, key: str, value: str) -> bool:
        """Persist a credential.  Returns ``True`` on success."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove a credential.  Returns ``True`` on success."""

    def is_available(self) -> bool:
        """Return ``True`` if this backend can be used right now."""
        return True


# ── Environment-variable backend ─────────────────────────────────────────────


class EnvBackend(CredentialBackend):
    """Read-only backend that resolves ``key`` from ``os.environ``.

    The lookup uppercases the key, so ``email_password`` becomes
    ``EMAIL_PASSWORD``.  This backend is always consulted as a fallback
    after the configured primary backend so users can override individual
    secrets without re-running setup.
    """

    name = "env"

    def get(self, key: str) -> str | None:
        return os.environ.get(key.upper()) or None

    def set(self, key: str, value: str) -> bool:
        # Env vars are not a write target.
        return False

    def delete(self, key: str) -> bool:
        return False


# ── macOS Keychain backend ───────────────────────────────────────────────────


class KeychainBackend(CredentialBackend):
    """macOS Keychain backend via the ``keyring`` library."""

    name = "keychain"

    def get(self, key: str) -> str | None:
        try:
            import keyring
            value = keyring.get_password(SERVICE_NAME, key)
            if value:
                logger.debug("credentials.keychain_hit", key=key)
            return value
        except Exception:
            return None

    def set(self, key: str, value: str) -> bool:
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, key, value)
            logger.info("credentials.stored", key=key, backend=self.name)
            return True
        except Exception as e:
            logger.warning("credentials.store_failed", key=key, error=str(e))
            return False

    def delete(self, key: str) -> bool:
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, key)
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        # Only meaningful on macOS.  On Linux/Windows the keyring library
        # may use a less-trusted backend, so we don't auto-select it.
        if platform.system() != "Darwin":
            return False
        try:
            import keyring  # noqa: F401
            return True
        except Exception:
            return False


# ── Encrypted file backend ───────────────────────────────────────────────────


class MasterKeyError(RuntimeError):
    """Raised when the encryption key cannot be obtained or is wrong."""


class EncryptedFileBackend(CredentialBackend):
    """AES-256-GCM encrypted JSON store on disk.

    File layout (JSON, ``0o600`` permissions)::

        {
          "version": 1,
          "kdf": "scrypt",
          "kdf_params": {"n": 32768, "r": 8, "p": 1},
          "salt": "<base64>",
          "nonce": "<base64>",
          "ciphertext": "<base64>"   // AES-GCM(plaintext = JSON map)
        }

    The plaintext is the entire credential map, re-encrypted with a
    fresh nonce on every write.  Encrypting the whole map at once is
    simpler than per-entry encryption and gives the same security
    properties for this use case.

    The master passphrase is resolved at first use via:

    1. ``SECURE_AGENTS_MASTER_KEY`` environment variable.
    2. Interactive ``getpass`` prompt (cached in process memory only).

    The derived key is cached on the instance for the rest of the
    process so subsequent reads do not have to re-derive or re-prompt.
    Set ``interactive=False`` to forbid prompting (used by background
    services that must fail rather than block).
    """

    name = "encrypted_file"

    def __init__(self, store_path: str | Path | None = None,
                 *, interactive: bool = True) -> None:
        self.store_path = Path(store_path).expanduser() if store_path else DEFAULT_STORE_PATH
        self._interactive = interactive
        self._cached_key: bytes | None = None
        self._cached_passphrase: str | None = None

    # ─── public API ──────────────────────────────────────────────────

    def is_available(self) -> bool:
        try:
            import cryptography  # noqa: F401
            return True
        except ImportError:
            return False

    def initialize(self, passphrase: str) -> bool:
        """Create an empty encrypted store with the given passphrase.

        Used by ``secure-agents auth init-store``.  Refuses to overwrite
        an existing store.
        """
        if self.store_path.exists():
            logger.warning("credentials.encfile_exists", path=str(self.store_path))
            return False
        if len(passphrase) < MIN_PASSPHRASE_LEN:
            raise ValueError(
                f"Master passphrase must be at least {MIN_PASSPHRASE_LEN} characters."
            )
        self._cached_passphrase = passphrase
        self._cached_key = None
        self._write_map({})
        return True

    def get(self, key: str) -> str | None:
        try:
            data = self._load_map()
        except MasterKeyError as e:
            logger.warning("credentials.encfile_unlock_failed", error=str(e))
            return None
        return data.get(key)

    def set(self, key: str, value: str) -> bool:
        try:
            data = self._load_map() if self.store_path.exists() else {}
        except MasterKeyError as e:
            logger.warning("credentials.encfile_unlock_failed", error=str(e))
            return False
        data[key] = value
        try:
            self._write_map(data)
        except MasterKeyError as e:
            logger.warning("credentials.encfile_write_failed", error=str(e))
            return False
        logger.info("credentials.stored", key=key, backend=self.name)
        return True

    def delete(self, key: str) -> bool:
        if not self.store_path.exists():
            return False
        try:
            data = self._load_map()
        except MasterKeyError:
            return False
        if key not in data:
            return False
        del data[key]
        try:
            self._write_map(data)
        except MasterKeyError:
            return False
        return True

    def list_keys(self) -> list[str]:
        """Return all stored credential names (no values)."""
        if not self.store_path.exists():
            return []
        try:
            return sorted(self._load_map().keys())
        except MasterKeyError:
            return []

    def lock(self) -> None:
        """Drop the cached key and passphrase from memory."""
        self._cached_key = None
        self._cached_passphrase = None

    # ─── internals ───────────────────────────────────────────────────

    def _resolve_passphrase(self) -> str:
        if self._cached_passphrase is not None:
            return self._cached_passphrase
        env_val = os.environ.get(MASTER_KEY_ENV)
        if env_val:
            self._cached_passphrase = env_val
            return env_val
        if not self._interactive or not sys.stdin.isatty():
            raise MasterKeyError(
                f"Master passphrase required.  Set {MASTER_KEY_ENV} or run "
                f"`secure-agents auth init-store` interactively."
            )
        prompt = (
            f"Master passphrase for {self.store_path}: "
            if self.store_path.exists()
            else f"Set master passphrase for {self.store_path}: "
        )
        passphrase = getpass.getpass(prompt)
        if not passphrase:
            raise MasterKeyError("No passphrase entered.")
        self._cached_passphrase = passphrase
        return passphrase

    def _derive_key(self, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return kdf.derive(self._resolve_passphrase().encode("utf-8"))

    def _check_permissions(self) -> None:
        if not self.store_path.exists():
            return
        # Refuse to read a world-readable / group-readable secret store.
        mode = self.store_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise MasterKeyError(
                f"Refusing to load {self.store_path}: insecure permissions {oct(mode)}. "
                f"Run `chmod 600 {self.store_path}` and retry."
            )

    def _load_map(self) -> dict[str, str]:
        self._check_permissions()
        if not self.store_path.exists():
            raise MasterKeyError(
                f"Encrypted credential store does not exist: {self.store_path}.  "
                f"Run `secure-agents auth init-store` to create one."
            )
        try:
            blob = json.loads(self.store_path.read_text())
        except json.JSONDecodeError as e:
            raise MasterKeyError(f"Credential store is corrupt: {e}") from e

        if blob.get("version") != _FILE_VERSION:
            raise MasterKeyError(
                f"Unsupported credential store version: {blob.get('version')!r}"
            )
        try:
            salt = base64.b64decode(blob["salt"])
            nonce = base64.b64decode(blob["nonce"])
            ciphertext = base64.b64decode(blob["ciphertext"])
        except (KeyError, ValueError) as e:
            raise MasterKeyError(f"Credential store is malformed: {e}") from e

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag

        if self._cached_key is None:
            self._cached_key = self._derive_key(salt)
        try:
            plaintext = AESGCM(self._cached_key).decrypt(nonce, ciphertext, None)
        except InvalidTag as e:
            # Wrong passphrase or tampered file.  Drop the cache so the
            # next attempt re-derives from a fresh prompt.
            self._cached_key = None
            self._cached_passphrase = None
            raise MasterKeyError(
                "Failed to decrypt credential store: wrong passphrase or tampered file."
            ) from e
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise MasterKeyError(f"Decrypted credential blob is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise MasterKeyError("Decrypted credential blob is not a JSON object.")
        return {str(k): str(v) for k, v in data.items()}

    def _write_map(self, data: dict[str, str]) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = secrets.token_bytes(_SALT_LEN)
        nonce = secrets.token_bytes(_NONCE_LEN)
        # Always derive a fresh key with a fresh salt on write so a
        # changed passphrase takes effect immediately.  Re-cache it.
        self._cached_key = self._derive_key(salt)
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(self._cached_key).encrypt(nonce, plaintext, None)

        blob: dict[str, Any] = {
            "version": _FILE_VERSION,
            "kdf": "scrypt",
            "kdf_params": {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P},
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to a temp file in the same directory then
        # rename, so a crash mid-write cannot corrupt the existing store.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".credentials-", suffix=".tmp",
            dir=str(self.store_path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(blob, f)
            os.replace(tmp_path, self.store_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            self.store_path.chmod(0o600)
        except OSError:
            pass


# ── Factory ──────────────────────────────────────────────────────────────────


def resolve_backend(
    name: str = "auto",
    store_path: str | Path | None = None,
    *,
    interactive: bool = True,
) -> CredentialBackend:
    """Build a backend by configuration name.

    ``name`` may be ``"keychain"``, ``"encrypted_file"``, or ``"auto"``.
    ``"auto"`` picks the strongest backend usable on the current host:
    Keychain on macOS, otherwise the encrypted file (which works on
    Linux VMs, containers, and headless servers).
    """
    name = (name or "auto").lower()
    if name == "keychain":
        return KeychainBackend()
    if name in ("encrypted_file", "encrypted-file", "file"):
        return EncryptedFileBackend(store_path, interactive=interactive)
    if name == "env":
        return EnvBackend()
    if name == "auto":
        kc = KeychainBackend()
        if kc.is_available():
            return kc
        return EncryptedFileBackend(store_path, interactive=interactive)
    raise ValueError(f"Unknown credential backend: {name!r}")
