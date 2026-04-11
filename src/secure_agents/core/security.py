"""Security utilities: file validation, filename sanitization, and audit logging.

Prompt-injection defense has been moved to a three-layer architecture:

    1. **Structured outputs** (``schemas.py``) — JSON-Schema-constrained LLM
       responses.  The primary defense: even if injection succeeds, the output
       shape is locked.
    2. **Validator LLM** (``validator.py``) — a secondary LLM call that screens
       untrusted input before it reaches the primary agent LLM.
    3. **Message boundaries** (``message_builder.py``) — API-level isolation
       that keeps untrusted content in clearly labelled user-role messages,
       separated from system instructions.

The old regex-based ``sanitize_text()`` function has been removed.  Regex
patterns are too easy to bypass (encoding tricks, homoglyphs, paraphrasing)
and too easy to over-match (flagging legitimate legal text).  The new
architecture defends at the *structural* level rather than trying to
pattern-match attack strings.

This module retains file-level security (magic-byte validation, path
containment, filename sanitization) and the audit log.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

# ── Magic byte signatures for file validation ────────────────────────────────

_MAGIC_SIGNATURES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".docx": [b"PK\x03\x04", b"PK\x05\x06"],  # ZIP format (OOXML)
    ".doc": [b"\xd0\xcf\x11\xe0"],              # OLE2 compound document
}


def validate_file(
    path: str | Path,
    allowed_types: list[str] | None = None,
    max_size_mb: int = 50,
) -> tuple[bool, str]:
    """Validate a file before processing.

    Checks:
    1. File exists and is a regular file
    2. Extension is in the allowlist
    3. File size is within limits
    4. Magic bytes match the claimed file type

    Returns:
        (is_valid, reason) — True if the file passes all checks.
    """
    path = Path(path)
    allowed = allowed_types or [".pdf", ".docx", ".doc"]

    if not path.exists():
        return False, f"File does not exist: {path.name}"

    if not path.is_file():
        return False, f"Not a file: {path.name}"

    # Check for path traversal in filename
    if ".." in path.parts:
        return False, "Path traversal detected"

    suffix = path.suffix.lower()
    if suffix not in allowed:
        return False, f"File type {suffix} not allowed. Allowed: {allowed}"

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_size_mb:
        return False, f"File too large: {size_mb:.1f}MB (max {max_size_mb}MB)"

    # Magic byte validation — verify file content matches claimed type
    if suffix in _MAGIC_SIGNATURES:
        try:
            with open(path, "rb") as f:
                header = f.read(16)
            signatures = _MAGIC_SIGNATURES[suffix]
            if not any(header.startswith(sig) for sig in signatures):
                logger.warning("security.magic_mismatch",
                             filename=path.name, suffix=suffix,
                             header_hex=header[:8].hex())
                return False, (
                    f"File content does not match {suffix} format. "
                    f"The file may be corrupted or have a wrong extension."
                )
        except OSError:
            return False, f"Cannot read file: {path.name}"

    return True, "OK"


def file_hash(path: str | Path) -> str:
    """Compute SHA-256 hash of a file for integrity tracking."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """Sanitize a filename to prevent path traversal and other attacks.

    - Only allows alphanumeric characters, dots, hyphens, and underscores
    - Enforces a maximum length
    - Rejects empty results
    - Prevents leading dots (hidden files)

    Returns:
        Sanitized filename, or "unnamed" if the result would be empty.
    """
    safe = "".join(c for c in filename if c.isalnum() or c in ".-_")
    safe = safe[:max_length]
    safe = safe.lstrip(".")
    if not safe:
        safe = "unnamed"
    return safe


def validate_path_within(path: Path, root: Path) -> bool:
    """Verify that a resolved path is within the expected root directory.

    Prevents path traversal attacks by resolving both paths to absolute
    and checking containment.
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        return str(resolved).startswith(str(root_resolved))
    except (OSError, ValueError):
        return False


class AuditLog:
    """Append-only audit trail.  Logs metadata only, never document content."""

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
        logger.info("audit.event", audit_event=event,
                    **{k: v for k, v in metadata.items() if k != "timestamp"})


def cleanup_temp_files(temp_dir: str | Path) -> int:
    """Remove all files in the temp directory.  Returns count of files removed."""
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
