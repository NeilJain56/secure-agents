"""Shared agent building and plugin discovery logic.

Used by both the CLI and the web UI server to avoid duplication.
Only local providers (Ollama) are supported — no cloud egress.
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

    Only the Ollama provider is supported. Cloud providers have been removed
    to ensure no data ever leaves the machine.
    """
    merged = config.get_agent_config(agent_name)

    # Resolve provider — only ollama is allowed
    provider_name = merged.get("provider", {}).get("override", config.provider.active)
    if provider_name != "ollama":
        raise ValueError(
            f"Provider '{provider_name}' is not allowed. "
            f"Only 'ollama' (local inference) is supported. "
            f"No data leaves your machine."
        )
    provider_cls = registry.get_provider(provider_name)
    provider_settings = config.provider.ollama
    provider_config = provider_settings.model_dump()
    agent_provider = merged.get("provider", {})
    if "model" in agent_provider:
        provider_config["model"] = agent_provider["model"]
    if "temperature" in agent_provider:
        provider_config["temperature"] = agent_provider["temperature"]
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
