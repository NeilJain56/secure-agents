"""Base agent interface.

Agents are thin orchestrators that compose tools and a provider to accomplish
a workflow. They contain only business logic, never direct I/O.
"""

from __future__ import annotations

import threading
import time
import structlog
from abc import ABC, abstractmethod

from secure_agents.core.base_provider import BaseProvider
from secure_agents.core.base_tool import BaseTool
from secure_agents.core.metrics import metrics

logger = structlog.get_logger()


class BaseAgent(ABC):
    """Abstract base class for all agents."""

    name: str = ""
    description: str = ""
    features: list[str] = []
    version: str = "0.1.0"

    def __init__(
        self,
        tools: dict[str, BaseTool],
        provider: BaseProvider,
        config: dict | None = None,
    ) -> None:
        self.tools = tools
        self.provider = provider
        self.config = config or {}
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()

    def setup(self) -> None:
        """Called once before the agent starts its run loop. Override if needed."""
        logger.info("agent.setup", agent=self.name)

    def run(self) -> None:
        """Start the agent's main loop.

        Safe to call from any thread. Use `request_stop()` to shut down
        gracefully from another thread or a signal handler.
        """
        self._stop_event.clear()
        logger.info("agent.started", agent=self.name)
        metrics.record_start(self.name)
        self.setup()

        try:
            while not self._stop_event.is_set():
                t0 = time.monotonic()
                try:
                    self.tick()
                    metrics.record_tick(self.name, time.monotonic() - t0)
                except Exception:
                    metrics.record_tick(self.name, time.monotonic() - t0)
                    metrics.record_error(self.name)
                    logger.exception("agent.tick_error", agent=self.name)
        finally:
            self.shutdown()

    def request_stop(self) -> None:
        """Signal this agent to stop. Thread-safe."""
        logger.info("agent.stop_requested", agent=self.name)
        self._stop_event.set()

    @abstractmethod
    def tick(self) -> None:
        """Execute one iteration of the agent's work loop.

        Called repeatedly while the agent is running.
        Use `self._stop_event.wait(seconds)` instead of `time.sleep()`
        so the agent can be stopped promptly.
        """
        ...

    def shutdown(self) -> None:
        """Called once when the agent stops. Override for cleanup."""
        self._stop_event.set()
        metrics.record_stop(self.name)
        logger.info("agent.shutdown", agent=self.name)

    def get_tool(self, name: str) -> BaseTool:
        """Get a tool by name from this agent's tool set."""
        if name not in self.tools:
            raise KeyError(f"Tool '{name}' not available. Agent has: {list(self.tools)}")
        return self.tools[name]

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} tools={list(self.tools)}>"
