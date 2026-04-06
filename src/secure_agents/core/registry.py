"""Plugin registry for agents, tools, and providers.

The registry is the architectural spine of the system. All agents, tools, and
providers register themselves via decorators and are discovered by name at runtime.

Usage:
    @register_tool("email_reader")
    class EmailReaderTool(BaseTool):
        ...

    # Later, retrieve by name:
    tool_cls = registry.get_tool("email_reader")
    tool = tool_cls(config)
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from secure_agents.core.base_agent import BaseAgent
    from secure_agents.core.base_tool import BaseTool
    from secure_agents.core.base_provider import BaseProvider


class Registry:
    """Central registry for all plugin types."""

    def __init__(self) -> None:
        self._agents: dict[str, type[BaseAgent]] = {}
        self._tools: dict[str, type[BaseTool]] = {}
        self._providers: dict[str, type[BaseProvider]] = {}
        self._tool_instances: dict[str, BaseTool] = {}

    # --- Registration ---

    def register_agent(self, name: str, cls: type[BaseAgent]) -> None:
        self._agents[name] = cls

    def register_tool(self, name: str, cls: type[BaseTool]) -> None:
        self._tools[name] = cls

    def register_provider(self, name: str, cls: type[BaseProvider]) -> None:
        self._providers[name] = cls

    # --- Lookup (classes) ---

    def get_agent(self, name: str) -> type[BaseAgent]:
        if name not in self._agents:
            raise KeyError(f"Agent '{name}' not registered. Available: {list(self._agents)}")
        return self._agents[name]

    def get_tool_class(self, name: str) -> type[BaseTool]:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not registered. Available: {list(self._tools)}")
        return self._tools[name]

    def get_provider(self, name: str) -> type[BaseProvider]:
        if name not in self._providers:
            raise KeyError(f"Provider '{name}' not registered. Available: {list(self._providers)}")
        return self._providers[name]

    # --- Tool instances ---

    def create_tool(self, name: str, config: dict | None = None) -> BaseTool:
        """Create a new tool instance with the given config."""
        cls = self.get_tool_class(name)
        return cls(config=config or {})

    def resolve_tools(self, tool_names: list[str], tool_configs: dict | None = None) -> dict[str, BaseTool]:
        """Create tool instances for a specific agent.

        Each agent gets its own tool instances so different agents can have
        different configs (e.g., different file size limits, different output dirs).
        """
        configs = tool_configs or {}
        return {
            name: self.create_tool(name, configs.get(name, {}))
            for name in tool_names
        }

    # --- Listing ---

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    # --- Auto-discovery ---

    def discover_plugins(self, package_name: str) -> None:
        """Import all submodules of a package to trigger @register decorators."""
        try:
            package = importlib.import_module(package_name)
        except ImportError:
            return
        if not hasattr(package, "__path__"):
            return
        for _importer, modname, ispkg in pkgutil.walk_packages(
            package.__path__, prefix=package.__name__ + "."
        ):
            try:
                importlib.import_module(modname)
            except ImportError:
                continue


# Global singleton
registry = Registry()


def register_agent(name: str):
    """Decorator to register an agent class."""
    def decorator(cls):
        registry.register_agent(name, cls)
        return cls
    return decorator


def register_tool(name: str):
    """Decorator to register a tool class."""
    def decorator(cls):
        registry.register_tool(name, cls)
        return cls
    return decorator


def register_provider(name: str):
    """Decorator to register a provider class."""
    def decorator(cls):
        registry.register_provider(name, cls)
        return cls
    return decorator
