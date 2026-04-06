"""Trigger system for launching agent work on events.

Triggers are the entry-point for agent execution. They watch for external
signals -- a cron-like schedule, a new file appearing on disk, or an explicit
manual invocation -- and fire a callback when the event occurs.

All triggers share a common lifecycle: ``start()`` to arm, ``stop()`` to
disarm. They are fully thread-safe and designed to be managed by
:class:`~secure_agents.core.trigger_manager.TriggerManager`.
"""

from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Interval parsing helper
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.IGNORECASE)

_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _parse_interval(value: int | str) -> float:
    """Parse a human-friendly interval string into seconds.

    Accepted formats:
        - ``300`` or ``"300"``  -- raw seconds
        - ``"every 5m"``       -- 5 minutes
        - ``"every 1h"``       -- 1 hour
        - ``"every 30s"``      -- 30 seconds
        - ``"every 2d"``       -- 2 days

    Raises :class:`ValueError` if the format is not recognised.
    """
    if isinstance(value, (int, float)):
        return float(value)

    value_str = str(value).strip()

    # Plain numeric string
    try:
        return float(value_str)
    except ValueError:
        pass

    match = _INTERVAL_RE.match(value_str)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        return float(amount * _UNIT_SECONDS[unit])

    raise ValueError(
        f"Cannot parse interval {value_str!r}. "
        "Expected an int (seconds) or 'every <N><s|m|h|d>'."
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class BaseTrigger(ABC):
    """Abstract base class for all trigger types."""

    def __init__(
        self,
        agent_name: str,
        config: dict[str, Any],
        callback: Callable[..., Any],
    ) -> None:
        self.agent_name = agent_name
        self.config = config
        self.callback = callback
        self._running = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        """Whether this trigger is currently active."""
        with self._lock:
            return self._running

    @abstractmethod
    def start(self) -> None:
        """Arm the trigger so it begins watching for events."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Disarm the trigger and release resources."""
        ...

    @property
    def trigger_type(self) -> str:
        """Human-readable trigger type name."""
        return self.__class__.__name__

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} agent={self.agent_name!r} "
            f"running={self.running}>"
        )


# ---------------------------------------------------------------------------
# CronTrigger -- recurring schedule via stdlib threading.Timer
# ---------------------------------------------------------------------------

class CronTrigger(BaseTrigger):
    """Schedule-based trigger using :class:`threading.Timer`.

    Fires the callback at a fixed interval.  The interval is parsed from
    ``config["interval"]`` which can be an int (seconds) or a string like
    ``"every 5m"``.

    The timer loop works as follows:
      1. ``start()`` schedules the first timer.
      2. When the timer fires it invokes the callback, records the next run
         time, and re-schedules itself.
      3. ``stop()`` cancels any pending timer.
    """

    def __init__(
        self,
        agent_name: str,
        config: dict[str, Any],
        callback: Callable[..., Any],
    ) -> None:
        super().__init__(agent_name, config, callback)
        self._interval: float = _parse_interval(config.get("interval", 60))
        self._timer: threading.Timer | None = None
        self._next_run_at: float | None = None

    @property
    def next_run_at(self) -> datetime | None:
        """UTC datetime of the next scheduled invocation, or *None*."""
        with self._lock:
            if self._next_run_at is None:
                return None
            return datetime.fromtimestamp(self._next_run_at, tz=timezone.utc)

    # -- lifecycle --

    def start(self) -> None:
        with self._lock:
            if self._running:
                logger.warning(
                    "trigger.already_running",
                    agent=self.agent_name,
                    trigger="cron",
                )
                return
            self._running = True
        logger.info(
            "trigger.cron.started",
            agent=self.agent_name,
            interval_s=self._interval,
        )
        self._schedule_next()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            timer = self._timer
            self._timer = None
            self._next_run_at = None

        if timer is not None:
            timer.cancel()

        logger.info("trigger.cron.stopped", agent=self.agent_name)

    # -- internal --

    def _schedule_next(self) -> None:
        """Schedule the next timer tick."""
        with self._lock:
            if not self._running:
                return
            self._next_run_at = time.time() + self._interval
            self._timer = threading.Timer(self._interval, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        """Called by the timer thread when the interval elapses."""
        with self._lock:
            if not self._running:
                return

        logger.info("trigger.cron.fired", agent=self.agent_name)
        try:
            self.callback()
        except Exception:
            logger.exception(
                "trigger.cron.callback_error",
                agent=self.agent_name,
            )

        # Re-arm for the next cycle
        self._schedule_next()


# ---------------------------------------------------------------------------
# FileWatchTrigger -- filesystem watcher via watchdog
# ---------------------------------------------------------------------------

class FileWatchTrigger(BaseTrigger):
    """Trigger that watches a directory for new files matching glob patterns.

    Uses the ``watchdog`` library (:class:`Observer` + :class:`FileSystemEventHandler`).

    Config keys:
        ``watch_dir``  -- directory path to watch (required).
        ``patterns``   -- list of glob patterns, e.g. ``["*.pdf", "*.docx"]``.
                          Defaults to ``["*"]`` (all files).
    """

    def __init__(
        self,
        agent_name: str,
        config: dict[str, Any],
        callback: Callable[..., Any],
    ) -> None:
        super().__init__(agent_name, config, callback)
        self._watch_dir = Path(config["watch_dir"]).resolve()
        self._patterns: list[str] = config.get("patterns", ["*"])
        self._observer: Any | None = None  # watchdog Observer (lazily imported)

    # -- lifecycle --

    def start(self) -> None:
        # Lazy import so the rest of the module works without watchdog
        # installed (only this trigger type needs it).
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent

        trigger = self  # capture for the inner class

        class _Handler(FileSystemEventHandler):
            """Relay file-creation events that match the configured patterns."""

            def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
                if event.is_directory:
                    return
                filepath = Path(event.src_path)
                if not trigger._matches(filepath):
                    return
                logger.info(
                    "trigger.filewatch.matched",
                    agent=trigger.agent_name,
                    path=str(filepath),
                )
                try:
                    trigger.callback(filepath=str(filepath))
                except Exception:
                    logger.exception(
                        "trigger.filewatch.callback_error",
                        agent=trigger.agent_name,
                        path=str(filepath),
                    )

        with self._lock:
            if self._running:
                logger.warning(
                    "trigger.already_running",
                    agent=self.agent_name,
                    trigger="filewatch",
                )
                return
            self._running = True

        if not self._watch_dir.is_dir():
            self._watch_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "trigger.filewatch.created_dir",
                agent=self.agent_name,
                path=str(self._watch_dir),
            )

        observer = Observer()
        observer.schedule(_Handler(), str(self._watch_dir), recursive=False)
        observer.daemon = True
        observer.start()

        with self._lock:
            self._observer = observer

        logger.info(
            "trigger.filewatch.started",
            agent=self.agent_name,
            watch_dir=str(self._watch_dir),
            patterns=self._patterns,
        )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            observer = self._observer
            self._observer = None

        if observer is not None:
            observer.stop()
            observer.join(timeout=5)

        logger.info("trigger.filewatch.stopped", agent=self.agent_name)

    # -- helpers --

    def _matches(self, filepath: Path) -> bool:
        """Return True if *filepath* matches any of the configured patterns."""
        from fnmatch import fnmatch

        name = filepath.name
        return any(fnmatch(name, pat) for pat in self._patterns)


# ---------------------------------------------------------------------------
# ManualTrigger -- placeholder for dashboard / API triggered runs
# ---------------------------------------------------------------------------

class ManualTrigger(BaseTrigger):
    """Trigger that only fires when explicitly invoked via :meth:`fire`.

    ``start()`` and ``stop()`` are no-ops since manual triggers do not
    automatically watch for events.
    """

    def start(self) -> None:
        with self._lock:
            self._running = True
        logger.info("trigger.manual.started", agent=self.agent_name)

    def stop(self) -> None:
        with self._lock:
            self._running = False
        logger.info("trigger.manual.stopped", agent=self.agent_name)

    def fire(self, **kwargs: Any) -> None:
        """Invoke the callback once.

        Parameters
        ----------
        **kwargs:
            Passed through to the callback.
        """
        logger.info("trigger.manual.fired", agent=self.agent_name)
        try:
            self.callback(**kwargs)
        except Exception:
            logger.exception(
                "trigger.manual.callback_error",
                agent=self.agent_name,
            )


# ---------------------------------------------------------------------------
# Public mapping for factory use
# ---------------------------------------------------------------------------

TRIGGER_TYPES: dict[str, type[BaseTrigger]] = {
    "cron": CronTrigger,
    "file_watch": FileWatchTrigger,
    "manual": ManualTrigger,
}
