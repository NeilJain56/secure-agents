"""Security utilities: file validation, input sanitization, and audit logging.

All security functions operate locally. Audit logs record metadata only,
never document content or PII.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Known prompt injection patterns to strip from document text
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?prior\s+(instructions|prompts)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
]


def validate_file(
    path: str | Path,
    allowed_types: list[str] | None = None,
    max_size_mb: int = 50,
) -> tuple[bool, str]:
    """Validate a file before processing.

    Returns:
        (is_valid, reason) - True if the file passes all checks.
    """
    path = Path(path)
    allowed = allowed_types or [".pdf", ".docx", ".doc"]

    if not path.exists():
        return False, f"File does not exist: {path}"

    if not path.is_file():
        return False, f"Not a file: {path}"

    suffix = path.suffix.lower()
    if suffix not in allowed:
        return False, f"File type {suffix} not allowed. Allowed: {allowed}"

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_size_mb:
        return False, f"File too large: {size_mb:.1f}MB (max {max_size_mb}MB)"

    return True, "OK"


def file_hash(path: str | Path) -> str:
    """Compute SHA-256 hash of a file for integrity tracking."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_text(text: str) -> str:
    """Remove potential prompt injection patterns from document text.

    This is a defense-in-depth measure. The system prompt hierarchy is the
    primary defense; sanitization is a secondary filter.
    """
    sanitized = text
    for pattern in _INJECTION_PATTERNS:
        sanitized = pattern.sub("[FILTERED]", sanitized)
    return sanitized


class AuditLog:
    """Append-only audit trail. Logs metadata only, never document content."""

    def __init__(self, log_path: str | Path = "./logs/audit.log") -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **metadata: str | int | float | bool | None) -> None:
        """Write an audit event."""
        entry = {
            "timestamp": time.time(),
            "event": event,
            **metadata,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("audit.event", audit_event=event, **{k: v for k, v in metadata.items() if k != "timestamp"})


def cleanup_temp_files(temp_dir: str | Path) -> int:
    """Remove all files in the temp directory. Returns count of files removed."""
    temp_dir = Path(temp_dir)
    if not temp_dir.exists():
        return 0
    count = 0
    for f in temp_dir.iterdir():
        if f.is_file():
            f.unlink()
            count += 1
    logger.info("security.cleanup", files_removed=count, dir=str(temp_dir))
    return count
