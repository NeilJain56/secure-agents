"""File storage tool - secure local storage for reports and outputs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.registry import register_tool

logger = structlog.get_logger()


@register_tool("file_storage")
class FileStorageTool(BaseTool):
    """Manages secure local file storage for agent outputs.

    Stores JSON reports, manages retention, and handles temp file cleanup.
    """

    name = "file_storage"
    description = "Store and retrieve files securely on the local filesystem"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.output_dir = Path(self.config.get("output_dir", "./output"))
        self.retention_days = self.config.get("retention_days", 90)
        self.output_dir.mkdir(parents=True, exist_ok=True)

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

        target_dir = self.output_dir / subfolder if subfolder else self.output_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        filepath = target_dir / filename

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info("file_storage.saved", path=str(filepath))
        return {"saved": True, "path": str(filepath)}

    def _load(self, kwargs: dict) -> dict:
        filename = kwargs.get("filename", "")
        subfolder = kwargs.get("subfolder", "")

        if not filename:
            return {"error": "No filename provided"}

        target_dir = self.output_dir / subfolder if subfolder else self.output_dir
        filepath = target_dir / filename

        if not filepath.exists():
            return {"error": f"File not found: {filepath}"}

        with open(filepath) as f:
            data = json.load(f)

        return {"data": data, "path": str(filepath)}

    def _list(self, kwargs: dict) -> dict:
        subfolder = kwargs.get("subfolder", "")
        target_dir = self.output_dir / subfolder if subfolder else self.output_dir

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
