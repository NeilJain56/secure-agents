"""Base tool interface.

Tools are reusable, self-contained units of functionality. They are registered
globally and shared across agents. Any agent can request any tool by name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """Abstract base class for all tools."""

    name: str = ""
    description: str = ""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    def execute(self, **kwargs: Any) -> dict:
        """Execute the tool's action.

        Args:
            **kwargs: Tool-specific arguments.

        Returns:
            Dict with tool results. Structure varies by tool.
        """
        ...

    def validate_config(self) -> bool:
        """Check that this tool has the config it needs. Override in subclasses."""
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
