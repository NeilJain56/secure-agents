"""Shared agent building and plugin discovery logic.

Used by both the CLI and the web UI server to avoid duplication.
Only local providers are supported — any provider class must declare
``local_only = True``.  This is enforced at build time.
"""

from __future__ import annotations

from secure_agents.core.config import AppConfig
from secure_agents.core.registry import registry


def discover_all() -> None:
    """Import all plugins so they register themselves."""
    registry.discover_plugins("secure_agents.providers")
    registry.discover_plugins("secure_agents.tools")
    registry.discover_plugins("secure_agents.agents")


def build_agent(agent_name: str, config: AppConfig):
    """Instantiate an agent with its tools and provider from merged config.

    Provider selection:
    - Default is the ``provider.active`` value from the top-level config.
    - An agent can override the provider via ``provider.override`` in its
      own config section (e.g. use a smaller/lighter model for one agent).
    - The selected provider class MUST declare ``local_only = True``;
      otherwise a ValueError is raised.  This is the on-prem guarantee.
    """
    merged = config.get_agent_config(agent_name)

    # Resolve provider — any registered local provider is allowed
    agent_provider_cfg = merged.get("provider", {}) or {}
    provider_name = agent_provider_cfg.get("override") or config.active_provider

    provider_cls = registry.get_provider(provider_name)

    # Enforce: provider must be local-only
    if not getattr(provider_cls, "local_only", False):
        raise ValueError(
            f"Provider '{provider_name}' is not declared local_only=True. "
            f"Secure Agents only supports on-prem/local inference providers. "
            f"No data leaves your machine."
        )

    # Build provider config: top-level settings + per-agent overrides
    provider_settings = config.get_provider_settings(provider_name)
    provider_config = provider_settings.model_dump()
    if "model" in agent_provider_cfg:
        provider_config["model"] = agent_provider_cfg["model"]
    if "temperature" in agent_provider_cfg:
        provider_config["temperature"] = agent_provider_cfg["temperature"]
    if "host" in agent_provider_cfg:
        provider_config["host"] = agent_provider_cfg["host"]
    provider = provider_cls(provider_config)

    # Resolve tools — each tool gets config from the merged agent config
    tool_names = merged.get("tools", [])
    email_cfg = merged.get("email", {})
    security_cfg = merged.get("security", {})
    storage_cfg = merged.get("storage", {})
    tool_configs = {
        "email_reader": email_cfg.get("imap", {}),
        "email_sender": email_cfg.get("smtp", {}),
        "document_parser": security_cfg,
        "file_storage": storage_cfg,
    }
    tools = registry.resolve_tools(tool_names, tool_configs)

    agent_cls = registry.get_agent(agent_name)
    return agent_cls(tools=tools, provider=provider, config=merged)
