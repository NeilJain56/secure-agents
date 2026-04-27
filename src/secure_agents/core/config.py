"""Configuration loading from YAML with environment variable interpolation.

Design principle: agent config is self-contained. Each agent entry under
``agents.<name>`` is a freeform dict that the agent owns completely. It can
specify its own tool settings, file size limits, polling intervals, etc.

The top-level ``defaults`` section provides fallback values that agents inherit
if they don't override them. This means adding a new agent never requires
touching global config sections.

Provider abstraction: any local LLM provider can be used (Ollama, llama.cpp,
vLLM, LM Studio, LocalAI, etc.).  The ``provider`` section configures the
active provider and its settings.  Each registered provider must declare
``local_only = True`` — the builder enforces this at runtime.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


def _interpolate_env(value: str) -> str:
    """Replace ${VAR:default} patterns with environment variable values."""
    pattern = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        return match.group(0)

    return pattern.sub(replacer, value)


def _interpolate_dict(data: dict | list | str) -> dict | list | str:
    """Recursively interpolate env vars in a config structure."""
    if isinstance(data, dict):
        return {k: _interpolate_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_interpolate_dict(item) for item in data]
    if isinstance(data, str):
        return _interpolate_env(data)
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on conflicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ProviderSettings(BaseModel):
    host: str = ""
    model: str = ""
    temperature: float = 0.1
    num_predict: int = -1   # max output tokens; -1 = unlimited (Ollama default)


class ProviderConfig(BaseModel):
    """Provider configuration.

    The ``active`` field names the provider to use (e.g. "ollama", "llamacpp",
    "vllm").  Provider-specific settings live under a key matching the
    provider name.  Any registered provider with ``local_only = True`` is
    allowed — the builder verifies this at runtime.
    """
    active: str = "ollama"
    ollama: ProviderSettings = ProviderSettings(host="http://localhost:11434", model="llama3.2")
    llamacpp: ProviderSettings = ProviderSettings(host="http://localhost:8080", model="default")
    vllm: ProviderSettings = ProviderSettings(host="http://localhost:8000", model="default")
    lmstudio: ProviderSettings = ProviderSettings(host="http://localhost:1234", model="default")
    localai: ProviderSettings = ProviderSettings(host="http://localhost:8080", model="default")


class QueueConfig(BaseModel):
    db_path: str = "./data/jobs.db"
    max_retries: int = 3
    retry_delay_seconds: int = 60


class CredentialsConfig(BaseModel):
    """Credential storage selection.

    ``backend`` chooses where secrets live:

    * ``"auto"`` -- Keychain on macOS, encrypted file everywhere else
      (recommended for VMs and headless servers).
    * ``"keychain"`` -- macOS Keychain via the ``keyring`` library.
    * ``"encrypted_file"`` -- AES-256-GCM encrypted JSON store under
      ``store_path``.  Master passphrase comes from
      ``SECURE_AGENTS_MASTER_KEY`` or an interactive prompt.

    The environment variable lookup is *always* consulted as a fallback
    after the configured backend, so users can override individual
    secrets with ``KEY=value`` without re-running setup.
    """
    backend: str = "auto"
    store_path: str = "~/.secure-agents/credentials.enc"


# Valid agent name pattern: lowercase alphanumeric + underscores, 1-64 chars
_AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_agent_name(name: str) -> bool:
    """Check that an agent name is safe for use in file paths and config keys."""
    return bool(_AGENT_NAME_PATTERN.match(name))


class AppConfig(BaseModel):
    """Top-level application config.

    - ``defaults``: shared fallback values for all agents
    - ``provider``: LLM provider selection and settings (local providers only)
    - ``queue``: job queue settings
    - ``agents``: per-agent config dicts; each inherits from defaults then overrides
    - ``pipelines``: named multi-agent pipelines (shown as a single tile in the dashboard
      and invocable as a single name in the CLI).  Each pipeline has a ``description``
      and an ordered list of ``agents`` that belong to it.
    """
    defaults: dict[str, Any] = {}
    provider: ProviderConfig = ProviderConfig()
    queue: QueueConfig = QueueConfig()
    credentials: CredentialsConfig = CredentialsConfig()
    agents: dict[str, dict[str, Any]] = {}
    pipelines: dict[str, dict[str, Any]] = {}
    max_workers: int = 4  # global maximum concurrent agent threads

    def get_agent_config(self, agent_name: str) -> dict[str, Any]:
        """Get the merged config for an agent: defaults + agent overrides."""
        agent_raw = self.agents.get(agent_name, {})
        return _deep_merge(self.defaults, agent_raw)

    def get_provider_settings(self, provider_name: str | None = None) -> ProviderSettings:
        """Get settings for a provider by name (defaults to the active provider)."""
        name = provider_name or self.active_provider
        if hasattr(self.provider, name):
            return getattr(self.provider, name)
        # Unknown provider — return default settings
        return ProviderSettings()

    @property
    def active_provider(self) -> str:
        return self.provider.active


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file.

    Environment variables in ${VAR:default} format are interpolated.
    Falls back to defaults if the file doesn't exist.

    Validates:
    - Agent names are safe for file paths
    """
    path = Path(path)

    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        raw = _interpolate_dict(raw)
    else:
        raw = {}

    config = AppConfig.model_validate(raw)

    # Validate agent names
    for name in config.agents:
        if not validate_agent_name(name):
            raise ValueError(
                f"Invalid agent name '{name}'. "
                f"Agent names must be lowercase alphanumeric + underscores, "
                f"start with a letter, and be 1-64 characters."
            )

    return config
