"""Structured logging setup.

Logs metadata only - never logs document content, email bodies, or PII.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import structlog

# Module-level singleton file handle so it only opens once across all calls.
_audit_fh: io.TextIOWrapper | None = None
_audit_lock = threading.Lock()


def _get_audit_fh(log_dir: str = "./logs") -> io.TextIOWrapper:
    """Open (or return cached) the audit log file handle."""
    global _audit_fh
    if _audit_fh is None:
        with _audit_lock:
            if _audit_fh is None:
                log_path = Path(log_dir) / "audit.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                _audit_fh = open(log_path, "a", buffering=1, encoding="utf-8")  # line-buffered
    return _audit_fh


class _FileJsonProcessor:
    """structlog processor that writes JSON lines to the audit log file."""

    def __init__(self, log_dir: str = "./logs") -> None:
        self._log_dir = log_dir

    def __call__(self, logger, method: str, event_dict: dict) -> dict:
        fh = _get_audit_fh(self._log_dir)
        try:
            fh.write(json.dumps(event_dict, default=str) + "\n")
        except Exception:
            pass  # Never let file I/O break the application
        return event_dict


def setup_logging(json_output: bool = False, log_dir: str = "./logs") -> None:
    """Configure structlog for the application.

    Always writes JSON lines to ``<log_dir>/audit.log`` (line-buffered) in
    addition to the normal console output.  The ``json_output`` flag controls
    whether the *console* renderer also emits JSON (useful for machine-readable
    stdout pipelines).
    """
    # Shared pre-processors applied before the output renderers.
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    # Tee to the audit log file (JSON lines) before the console renderer runs.
    processors = shared_processors + [
        _FileJsonProcessor(log_dir=log_dir),
    ]

    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
