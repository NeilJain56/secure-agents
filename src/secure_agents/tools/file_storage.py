"""File storage tool - secure local storage for reports and outputs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.registry import register_tool
from secure_agents.core.security import sanitize_filename, validate_path_within

logger = structlog.get_logger()


@register_tool("file_storage")
class FileStorageTool(BaseTool):
    """Manages secure local file storage for agent outputs.

    All file operations are jailed within the configured output_dir.
    Path traversal attempts are rejected.
    """

    name = "file_storage"
    description = "Store and retrieve files securely on the local filesystem"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.output_dir = Path(self.config.get("output_dir", "./output")).resolve()
        self.retention_days = self.config.get("retention_days", 90)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _safe_target(self, filename: str, subfolder: str = "") -> Path | None:
        """Resolve and validate a target path, ensuring it's within output_dir.

        Returns None if the path is unsafe (path traversal, absolute path, etc.).
        """
        # Reject any directory traversal or absolute paths in inputs
        for component in [filename, subfolder]:
            if ".." in component or component.startswith("/") or component.startswith("\\"):
                logger.warning("file_storage.path_traversal_blocked",
                             filename=filename, subfolder=subfolder)
                return None
            if "\x00" in component:
                logger.warning("file_storage.null_byte_blocked",
                             filename=filename, subfolder=subfolder)
                return None

        # Sanitize the filename
        safe_name = sanitize_filename(filename)
        if not safe_name or safe_name == "unnamed":
            return None

        # Build and resolve the full path
        if subfolder:
            safe_subfolder = sanitize_filename(subfolder)
            target_dir = self.output_dir / safe_subfolder
        else:
            target_dir = self.output_dir

        filepath = (target_dir / safe_name).resolve()

        # Final containment check: is the resolved path still under output_dir?
        if not validate_path_within(filepath, self.output_dir):
            logger.warning("file_storage.path_escape_blocked",
                         resolved=str(filepath), output_dir=str(self.output_dir))
            return None

        return filepath

    def execute(self, **kwargs: Any) -> dict:
        """Store or retrieve a file.

        kwargs:
            action: "save" | "load" | "list" | "cleanup" (required)
            filename: Name of the file (required for save/load)
            data: Dict to save as JSON (required for save)
            subfolder: Optional subfolder within output_dir

        Returns:
            Varies by action.
        """
        action = kwargs.get("action", "")

        if action == "save":
            return self._save(kwargs)
        elif action == "load":
            return self._load(kwargs)
        elif action == "list":
            return self._list(kwargs)
        elif action == "cleanup":
            return self._cleanup()
        else:
            return {"error": f"Unknown action: {action}. Use: save, load, list, cleanup"}

    def _save(self, kwargs: dict) -> dict:
        filename = kwargs.get("filename", "")
        data = kwargs.get("data", {})
        subfolder = kwargs.get("subfolder", "")

        if not filename:
            return {"error": "No filename provided"}

        filepath = self._safe_target(filename, subfolder)
        if filepath is None:
            return {"error": "Invalid filename or path traversal detected"}

        filepath.parent.mkdir(parents=True, exist_ok=True)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info("file_storage.saved", path=str(filepath))
        return {"saved": True, "path": str(filepath)}

    def _load(self, kwargs: dict) -> dict:
        filename = kwargs.get("filename", "")
        subfolder = kwargs.get("subfolder", "")

        if not filename:
            return {"error": "No filename provided"}

        filepath = self._safe_target(filename, subfolder)
        if filepath is None:
            return {"error": "Invalid filename or path traversal detected"}

        if not filepath.exists():
            return {"error": f"File not found: {filepath.name}"}

        with open(filepath) as f:
            data = json.load(f)

        return {"data": data, "path": str(filepath)}

    def _list(self, kwargs: dict) -> dict:
        subfolder = kwargs.get("subfolder", "")

        if subfolder:
            safe_subfolder = sanitize_filename(subfolder)
            target_dir = (self.output_dir / safe_subfolder).resolve()
            if not validate_path_within(target_dir, self.output_dir):
                return {"error": "Invalid subfolder"}
        else:
            target_dir = self.output_dir

        if not target_dir.exists():
            return {"files": []}

        files = []
        for f in sorted(target_dir.iterdir()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
        return {"files": files}

    def _cleanup(self) -> dict:
        """Remove files older than retention_days."""
        cutoff = time.time() - (self.retention_days * 86400)
        removed = 0

        for f in self.output_dir.rglob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1

        logger.info("file_storage.cleanup", removed=removed, retention_days=self.retention_days)
        return {"removed": removed}

    def validate_config(self) -> bool:
        return self.output_dir.exists() or self.output_dir.parent.exists()
