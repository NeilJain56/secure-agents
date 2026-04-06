"""Centralised manager for agent triggers.

The :class:`TriggerManager` owns the lifecycle of all triggers in the system.
It acts as a factory (creating the right :class:`BaseTrigger` subclass from a
config dict) and as a supervisor (starting, stopping, and listing triggers).

Typical usage from the application entry-point::

    from secure_agents.core.trigger_manager import TriggerManager

    manager = TriggerManager()
    manager.register("nda_reviewer", trigger_cfg, callback=my_callback)
    manager.start_all()
    ...
    manager.stop_all()
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

import structlog

from secure_agents.core.triggers import (
    BaseTrigger,
    CronTrigger,
    TRIGGER_TYPES,
)

logger = structlog.get_logger()


class TriggerManager:
    """Registry and lifecycle manager for all agent triggers.

    Thread-safe: all mutations go through ``_lock``.
    """

    def __init__(self) -> None:
        self._triggers: dict[str, BaseTrigger] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @staticmethod
    def _create_trigger(
        agent_name: str,
        trigger_config: dict[str, Any],
        callback: Callable[..., Any],
    ) -> BaseTrigger:
        """Instantiate the correct :class:`BaseTrigger` subclass.

        Parameters
        ----------
        agent_name:
            Logical name of the owning agent.
        trigger_config:
            Must contain a ``"type"`` key (one of ``"cron"``,
            ``"file_watch"``, or ``"manual"``).  The rest of the dict is
            forwarded as config to the trigger constructor.
        callback:
            Callable invoked when the trigger fires.

        Raises
        ------
        ValueError
            If the trigger ``type`` is missing or unrecognised.
        """
        trigger_type = trigger_config.get("type")
        if trigger_type is None:
            raise ValueError(
                f"Trigger config for agent {agent_name!r} is missing 'type'. "
                f"Available types: {list(TRIGGER_TYPES)}"
            )

        cls = TRIGGER_TYPES.get(trigger_type)
        if cls is None:
            raise ValueError(
                f"Unknown trigger type {trigger_type!r} for agent {agent_name!r}. "
                f"Available types: {list(TRIGGER_TYPES)}"
            )

        return cls(agent_name=agent_name, config=trigger_config, callback=callback)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        agent_name: str,
        trigger_config: dict[str, Any],
        callback: Callable[..., Any],
    ) -> BaseTrigger:
        """Create and register a trigger for *agent_name*.

        If a trigger is already registered for that agent it will be stopped
        and replaced.

        Returns the newly created :class:`BaseTrigger` instance.
        """
        with self._lock:
            existing = self._triggers.get(agent_name)

        # Stop outside the lock to avoid holding it during I/O.
        if existing is not None:
            logger.info(
                "trigger_manager.replacing",
                agent=agent_name,
                old_type=existing.trigger_type,
            )
            existing.stop()

        trigger = self._create_trigger(agent_name, trigger_config, callback)

        with self._lock:
            self._triggers[agent_name] = trigger

        logger.info(
            "trigger_manager.registered",
            agent=agent_name,
            trigger_type=trigger.trigger_type,
        )
        return trigger

    # ------------------------------------------------------------------
    # Lifecycle -- individual
    # ------------------------------------------------------------------

    def start(self, agent_name: str) -> None:
        """Start the trigger for a single agent."""
        trigger = self._get(agent_name)
        trigger.start()
        logger.info("trigger_manager.started", agent=agent_name)

    def stop(self, agent_name: str) -> None:
        """Stop the trigger for a single agent."""
        trigger = self._get(agent_name)
        trigger.stop()
        logger.info("trigger_manager.stopped", agent=agent_name)

    # ------------------------------------------------------------------
    # Lifecycle -- bulk
    # ------------------------------------------------------------------

    def start_all(self) -> None:
        """Start every registered trigger."""
        with self._lock:
            names = list(self._triggers)

        for name in names:
            try:
                self._triggers[name].start()
            except Exception:
                logger.exception("trigger_manager.start_failed", agent=name)

        logger.info("trigger_manager.started_all", count=len(names))

    def stop_all(self) -> None:
        """Stop every registered trigger."""
        with self._lock:
            names = list(self._triggers)

        for name in names:
            try:
                self._triggers[name].stop()
            except Exception:
                logger.exception("trigger_manager.stop_failed", agent=name)

        logger.info("trigger_manager.stopped_all", count=len(names))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_trigger(self, agent_name: str) -> BaseTrigger:
        """Return the trigger instance for *agent_name*."""
        return self._get(agent_name)

    def list_triggers(self) -> list[dict[str, Any]]:
        """Return a summary of all registered triggers.

        Each entry is a dict with keys:
            ``name``     -- agent name
            ``type``     -- trigger class name
            ``running``  -- bool
            ``next_run`` -- ISO datetime string or ``None`` (cron triggers only)
        """
        with self._lock:
            items = list(self._triggers.items())

        result: list[dict[str, Any]] = []
        for name, trigger in items:
            next_run: str | None = None
            if isinstance(trigger, CronTrigger):
                dt = trigger.next_run_at
                if dt is not None:
                    next_run = dt.isoformat()

            result.append(
                {
                    "name": name,
                    "type": trigger.trigger_type,
                    "running": trigger.running,
                    "next_run": next_run,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get(self, agent_name: str) -> BaseTrigger:
        with self._lock:
            trigger = self._triggers.get(agent_name)
        if trigger is None:
            raise KeyError(
                f"No trigger registered for agent {agent_name!r}. "
                f"Registered: {list(self._triggers)}"
            )
        return trigger

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._triggers)
        return f"<TriggerManager triggers={count}>"
