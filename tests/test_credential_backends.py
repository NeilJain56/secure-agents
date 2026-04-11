"""Tests for the credential backend abstraction.

Focus areas:

* The encrypted file backend round-trips secrets.
* It refuses to read a world-readable / group-readable store.
* A wrong passphrase fails closed (no plaintext leaks, cache cleared).
* A tampered ciphertext is detected via the AES-GCM tag.
* `resolve_backend("auto")` falls back to the encrypted file backend on
  hosts that lack a usable Keychain.
* The env backend is read-only and uppercases keys.
* `configure_credentials()` swaps the active backend at runtime and the
  env-var fallback still works.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from secure_agents.core import credentials as creds_mod
from secure_agents.core.credential_backends import (
    DEFAULT_STORE_PATH,
    MASTER_KEY_ENV,
    EncryptedFileBackend,
    EnvBackend,
    KeychainBackend,
    MasterKeyError,
    resolve_backend,
)


# ─── EncryptedFileBackend ────────────────────────────────────────────────────


def _new_backend(tmp_path: Path, passphrase: str = "correct-horse-battery-staple") -> EncryptedFileBackend:
    """Build a fresh encrypted backend pointed at tmp_path with the given passphrase."""
    store = tmp_path / "credentials.enc"
    backend = EncryptedFileBackend(store)
    backend.initialize(passphrase)
    return backend


def test_encrypted_file_init_and_round_trip(tmp_path):
    backend = _new_backend(tmp_path)
    assert backend.set("email_password", "hunter2") is True
    assert backend.set("anthropic_api_key", "sk-ant-123") is True

    # New instance using the same passphrase should be able to read both.
    fresh = EncryptedFileBackend(tmp_path / "credentials.enc")
    fresh._cached_passphrase = "correct-horse-battery-staple"
    assert fresh.get("email_password") == "hunter2"
    assert fresh.get("anthropic_api_key") == "sk-ant-123"
    assert sorted(fresh.list_keys()) == ["anthropic_api_key", "email_password"]


def test_encrypted_file_persists_with_0600_permissions(tmp_path):
    backend = _new_backend(tmp_path)
    backend.set("k", "v")
    mode = backend.store_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_encrypted_file_refuses_world_readable_store(tmp_path):
    backend = _new_backend(tmp_path)
    backend.set("k", "v")
    # Make it world-readable; loading must refuse.
    os.chmod(backend.store_path, 0o644)
    fresh = EncryptedFileBackend(backend.store_path)
    fresh._cached_passphrase = "correct-horse-battery-staple"
    # get() swallows MasterKeyError and returns None — but list_keys also
    # uses _load_map; we exercise the underlying error path directly too.
    assert fresh.get("k") is None
    with pytest.raises(MasterKeyError, match="insecure permissions"):
        fresh._load_map()


def test_encrypted_file_wrong_passphrase_fails_closed(tmp_path):
    backend = _new_backend(tmp_path, passphrase="real-passphrase-123")
    backend.set("k", "secret-value")

    fresh = EncryptedFileBackend(backend.store_path)
    fresh._cached_passphrase = "wrong-passphrase-456"
    # get() returns None and the cache is cleared so the next attempt
    # can re-prompt.
    assert fresh.get("k") is None
    assert fresh._cached_key is None
    assert fresh._cached_passphrase is None


def test_encrypted_file_tampered_ciphertext_is_detected(tmp_path):
    backend = _new_backend(tmp_path, passphrase="solid-passphrase-xyz")
    backend.set("k", "v")

    blob = json.loads(backend.store_path.read_text())
    blob["ciphertext"] = blob["ciphertext"][:-4] + "AAAA"
    backend.store_path.write_text(json.dumps(blob))

    fresh = EncryptedFileBackend(backend.store_path)
    fresh._cached_passphrase = "solid-passphrase-xyz"
    assert fresh.get("k") is None  # tag verification fails


def test_encrypted_file_delete(tmp_path):
    backend = _new_backend(tmp_path)
    backend.set("k", "v")
    backend.set("k2", "v2")
    assert backend.delete("k") is True
    assert backend.get("k") is None
    assert backend.get("k2") == "v2"


def test_encrypted_file_initialize_refuses_overwrite(tmp_path):
    backend = _new_backend(tmp_path)
    backend.set("k", "v")
    second = EncryptedFileBackend(backend.store_path)
    assert second.initialize("another-passphrase") is False


def test_encrypted_file_initialize_rejects_short_passphrase(tmp_path):
    backend = EncryptedFileBackend(tmp_path / "credentials.enc")
    with pytest.raises(ValueError, match="at least"):
        backend.initialize("short")


def test_encrypted_file_uses_env_var_for_passphrase(tmp_path, monkeypatch):
    store = tmp_path / "credentials.enc"
    monkeypatch.setenv(MASTER_KEY_ENV, "env-driven-passphrase")
    backend = EncryptedFileBackend(store, interactive=False)
    backend.initialize("env-driven-passphrase")
    backend.set("k", "v")

    fresh = EncryptedFileBackend(store, interactive=False)
    assert fresh.get("k") == "v"


def test_encrypted_file_non_interactive_without_env_fails(tmp_path, monkeypatch):
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    store = tmp_path / "credentials.enc"
    # Pre-create a store so we don't get the "does not exist" error first.
    init = EncryptedFileBackend(store)
    init.initialize("a-real-passphrase")

    fresh = EncryptedFileBackend(store, interactive=False)
    # No cached passphrase, no env var, no tty -> MasterKeyError underneath,
    # which get() converts to None.
    assert fresh.get("anything") is None
    with pytest.raises(MasterKeyError):
        fresh._resolve_passphrase()


def test_encrypted_file_lock_clears_cache(tmp_path):
    backend = _new_backend(tmp_path)
    backend.set("k", "v")
    assert backend._cached_key is not None
    backend.lock()
    assert backend._cached_key is None
    assert backend._cached_passphrase is None


# ─── EnvBackend ──────────────────────────────────────────────────────────────


def test_env_backend_uppercases_key(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "from-env")
    backend = EnvBackend()
    assert backend.get("email_password") == "from-env"
    assert backend.get("missing") is None


def test_env_backend_is_read_only(monkeypatch):
    backend = EnvBackend()
    assert backend.set("k", "v") is False
    assert backend.delete("k") is False


# ─── resolve_backend / auto selection ────────────────────────────────────────


def test_resolve_backend_explicit_names(tmp_path):
    assert resolve_backend("encrypted_file", tmp_path / "x.enc").name == "encrypted_file"
    assert resolve_backend("env").name == "env"


def test_resolve_backend_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown credential backend"):
        resolve_backend("nope")


def test_resolve_backend_auto_falls_back_to_encrypted_file(monkeypatch, tmp_path):
    """When the Keychain backend reports unavailable, auto picks the file backend."""
    monkeypatch.setattr(KeychainBackend, "is_available", lambda self: False)
    backend = resolve_backend("auto", tmp_path / "credentials.enc")
    assert isinstance(backend, EncryptedFileBackend)


def test_resolve_backend_auto_prefers_keychain_when_available(monkeypatch):
    monkeypatch.setattr(KeychainBackend, "is_available", lambda self: True)
    backend = resolve_backend("auto")
    assert isinstance(backend, KeychainBackend)


# ─── credentials module integration ──────────────────────────────────────────


def test_configure_credentials_sets_active_backend(tmp_path, monkeypatch):
    creds_mod._active_backend = None
    monkeypatch.setenv(MASTER_KEY_ENV, "another-strong-passphrase")
    backend = EncryptedFileBackend(tmp_path / "credentials.enc", interactive=False)
    backend.initialize("another-strong-passphrase")
    backend.set("email_password", "hunter2")

    creds_mod.configure_credentials(
        backend="encrypted_file",
        store_path=str(tmp_path / "credentials.enc"),
        interactive=False,
    )
    assert creds_mod.get_credential("email_password") == "hunter2"


def test_get_credential_falls_back_to_env(tmp_path, monkeypatch):
    """An env var should win when the active backend has nothing for the key."""
    monkeypatch.setenv(MASTER_KEY_ENV, "yet-another-passphrase")
    monkeypatch.setenv("CUSTOM_TOOL_TOKEN", "from-env")
    backend = EncryptedFileBackend(tmp_path / "credentials.enc", interactive=False)
    backend.initialize("yet-another-passphrase")
    creds_mod._active_backend = backend

    assert creds_mod.get_credential("custom_tool_token") == "from-env"


def test_store_credential_uses_active_backend(tmp_path, monkeypatch):
    monkeypatch.setenv(MASTER_KEY_ENV, "writeback-passphrase")
    backend = EncryptedFileBackend(tmp_path / "credentials.enc", interactive=False)
    backend.initialize("writeback-passphrase")
    creds_mod._active_backend = backend

    assert creds_mod.store_credential("api_key", "value") is True
    assert creds_mod.get_credential("api_key") == "value"
    assert creds_mod.delete_credential("api_key") is True
    assert creds_mod.get_credential("api_key") is None


def test_keychain_backend_is_unavailable_off_macos(monkeypatch):
    """Sanity: KeychainBackend.is_available() must depend on the platform."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert KeychainBackend().is_available() is False


# ─── DEFAULT_STORE_PATH sanity ───────────────────────────────────────────────


def test_default_store_path_is_under_user_home():
    assert str(DEFAULT_STORE_PATH).startswith(str(Path("~").expanduser()))
