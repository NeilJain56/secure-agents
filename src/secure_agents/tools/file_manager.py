"""File manager tool for the document sorting pipeline.

Provides directory scanning, file copying, directory creation, and CSV
writing.  All write operations are jailed within a configurable root
directory to prevent path traversal.  Read operations (scan) can access
any path the process can read — this is intentional because the source
folder for the sorting pipeline lives outside the output root.
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path
from typing import Any

import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.registry import register_tool

logger = structlog.get_logger()


def _is_within(path: Path, root: Path) -> bool:
    """Return True if *path* is inside *root* after resolution."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@register_tool("file_manager")
class FileManagerTool(BaseTool):
    """File operations for the document-sorting pipeline.

    Actions: scan, copy, mkdir, write_csv.

    Write operations (copy, mkdir, write_csv) are jailed inside the
    ``output_root`` config value so agents can never write outside of it.
    The ``scan`` action can read any directory the process has access to
    because the unsorted source folder lives outside the output root.
    """

    name = "file_manager"
    description = "Scan directories, copy files, create folders, write CSVs"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.output_root = Path(self.config.get("output_root", "./ai_generated")).resolve()

    def execute(self, **kwargs: Any) -> dict:
        action = kwargs.get("action", "")
        if action == "scan":
            return self._scan(kwargs)
        if action == "copy":
            return self._copy(kwargs)
        if action == "mkdir":
            return self._mkdir(kwargs)
        if action == "write_csv":
            return self._write_csv(kwargs)
        return {"error": f"Unknown action: {action}. Use: scan, copy, mkdir, write_csv"}

    # ── scan ─────────────────────────────────────────────────────────────

    def _scan(self, kwargs: dict) -> dict:
        """List files in a folder, optionally filtered by extension.

        kwargs:
            folder: str (required)
            extensions: list[str] — e.g. [".pdf", ".docx"]  (optional)
        """
        folder = kwargs.get("folder", "")
        if not folder:
            return {"error": "No folder provided"}

        path = Path(folder).resolve()
        if not path.is_dir():
            return {"error": f"Not a directory: {folder}"}

        extensions = kwargs.get("extensions")
        if extensions:
            extensions = {e.lower() for e in extensions}

        files = []
        for f in sorted(path.iterdir()):
            if not f.is_file():
                continue
            if extensions and f.suffix.lower() not in extensions:
                continue
            files.append({
                "name": f.name,
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "ext": f.suffix.lower(),
            })

        logger.info("file_manager.scan", folder=str(path), count=len(files))
        return {"files": files}

    # ── copy ─────────────────────────────────────────────────────────────

    def _copy(self, kwargs: dict) -> dict:
        """Copy a file into the output root.

        kwargs:
            src: str — source file path (required)
            dest: str — destination path *relative to output_root*, or
                        absolute but must resolve inside output_root (required)
        """
        src_raw = kwargs.get("src", "")
        dest_raw = kwargs.get("dest", "")
        if not src_raw or not dest_raw:
            return {"error": "Both 'src' and 'dest' are required"}

        src = Path(src_raw).resolve()
        if not src.is_file():
            return {"error": f"Source file not found: {src_raw}"}

        # Resolve dest relative to output_root if it is not absolute
        dest = Path(dest_raw)
        if not dest.is_absolute():
            dest = self.output_root / dest
        dest = dest.resolve()

        if not _is_within(dest, self.output_root):
            logger.warning("file_manager.copy_path_escape", dest=str(dest))
            return {"error": "Destination is outside the output root"}

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
        logger.info("file_manager.copied", src=src.name, dest=str(dest))
        return {"copied": True, "dest_path": str(dest)}

    # ── mkdir ────────────────────────────────────────────────────────────

    def _mkdir(self, kwargs: dict) -> dict:
        """Create a directory (relative to output_root).

        kwargs:
            path: str (required)
        """
        raw = kwargs.get("path", "")
        if not raw:
            return {"error": "No path provided"}

        target = Path(raw)
        if not target.is_absolute():
            target = self.output_root / target
        target = target.resolve()

        if not _is_within(target, self.output_root):
            logger.warning("file_manager.mkdir_path_escape", path=str(target))
            return {"error": "Path is outside the output root"}

        target.mkdir(parents=True, exist_ok=True)
        logger.info("file_manager.mkdir", path=str(target))
        return {"created": True, "path": str(target)}

    # ── write_csv ────────────────────────────────────────────────────────

    def _write_csv(self, kwargs: dict) -> dict:
        """Write a CSV file inside the output root.

        kwargs:
            path: str — file path relative to output_root (required)
            headers: list[str] (required)
            rows: list[list[str]] (required)
        """
        raw_path = kwargs.get("path", "")
        headers = kwargs.get("headers", [])
        rows = kwargs.get("rows", [])

        if not raw_path:
            return {"error": "No path provided"}
        if not headers:
            return {"error": "No headers provided"}

        target = Path(raw_path)
        if not target.is_absolute():
            target = self.output_root / target
        target = target.resolve()

        if not _is_within(target, self.output_root):
            logger.warning("file_manager.csv_path_escape", path=str(target))
            return {"error": "Path is outside the output root"}

        target.parent.mkdir(parents=True, exist_ok=True)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(rows)

        target.write_text(buf.getvalue(), encoding="utf-8")
        logger.info("file_manager.csv_written", path=str(target), rows=len(rows))
        return {"written": True, "path": str(target), "row_count": len(rows)}

    def validate_config(self) -> bool:
        return True
