"""Security utilities: file validation, input sanitization, and audit logging.

All security functions operate locally. Audit logs record metadata only,
never document content or PII.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

import structlog

logger = structlog.get_logger()

# ── Magic byte signatures for file validation ────────────────────────────────

_MAGIC_SIGNATURES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".docx": [b"PK\x03\x04", b"PK\x05\x06"],  # ZIP format (OOXML)
    ".doc": [b"\xd0\xcf\x11\xe0"],              # OLE2 compound document
}

# ── Prompt injection patterns (defense-in-depth) ─────────────────────────────
# This is a secondary filter. The system prompt hierarchy is the primary defense.
# These patterns are stripped from document text before it reaches the LLM.

_INJECTION_PATTERNS = [
    # Instruction override attempts
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|context)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|context)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|context)", re.IGNORECASE),
    re.compile(r"override\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|context)", re.IGNORECASE),
    re.compile(r"do\s+not\s+follow\s+(previous|prior|above|earlier)\s+(instructions|prompts)", re.IGNORECASE),
    re.compile(r"stop\s+being\s+an?\s+", re.IGNORECASE),

    # Role switching / identity manipulation
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:a|an|if)\s+", re.IGNORECASE),
    re.compile(r"pretend\s+(?:you(?:'re|\s+are)\s+|to\s+be\s+)", re.IGNORECASE),
    re.compile(r"from\s+now\s+on\s*,?\s*you", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"updated?\s+instructions?\s*:", re.IGNORECASE),

    # System prompt injection
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"\[system\]", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"<<\s*SYS\s*>>", re.IGNORECASE),

    # Data exfiltration attempts
    re.compile(r"(output|print|show|reveal|display)\s+(the\s+)?(system\s+prompt|instructions|configuration)", re.IGNORECASE),
    re.compile(r"what\s+(are|were)\s+your\s+(original\s+)?(instructions|prompts)", re.IGNORECASE),

    # Encoded/obfuscated injection attempts
    re.compile(r"base64\s*:", re.IGNORECASE),
    re.compile(r"eval\s*\(", re.IGNORECASE),
    re.compile(r"exec\s*\(", re.IGNORECASE),
]


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
        (is_valid, reason) - True if the file passes all checks.
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


def sanitize_text(text: str) -> str:
    """Remove potential prompt injection patterns from document text.

    This is a defense-in-depth measure. The system prompt hierarchy is the
    primary defense; sanitization is a secondary filter.

    Steps:
    1. Normalize Unicode to prevent homoglyph-based bypasses
    2. Strip known injection patterns
    3. Log when injections are detected (audit trail)
    """
    # Normalize Unicode to catch homoglyph/lookalike bypasses
    sanitized = unicodedata.normalize("NFKC", text)

    injection_count = 0
    for pattern in _INJECTION_PATTERNS:
        sanitized, n = pattern.subn("[FILTERED]", sanitized)
        injection_count += n

    if injection_count > 0:
        logger.warning("security.injection_filtered",
                      patterns_matched=injection_count,
                      text_length=len(text))

    return sanitized


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """Sanitize a filename to prevent path traversal and other attacks.

    - Only allows alphanumeric characters, dots, hyphens, and underscores
    - Enforces a maximum length
    - Rejects empty results
    - Prevents leading dots (hidden files)

    Returns:
        Sanitized filename, or "unnamed" if the result would be empty.
    """
    # Strip to safe characters only
    safe = "".join(c for c in filename if c.isalnum() or c in ".-_")

    # Enforce max length
    safe = safe[:max_length]

    # Prevent leading dots (hidden files / directory traversal)
    safe = safe.lstrip(".")

    # Prevent empty filenames
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
