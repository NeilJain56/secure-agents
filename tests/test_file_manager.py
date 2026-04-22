"""Tests for the file_manager tool."""

import csv
from pathlib import Path

import pytest

from secure_agents.tools.file_manager import FileManagerTool


@pytest.fixture
def tool(tmp_path):
    """FileManagerTool with output_root set to a temp directory."""
    return FileManagerTool(config={"output_root": str(tmp_path / "output")})


# ── scan ─────────────────────────────────────────────────────────────────────

def test_scan_lists_files(tmp_path, tool):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"%PDF")
    (folder / "b.docx").write_bytes(b"PK\x03\x04")
    (folder / "c.txt").write_text("hello")

    result = tool.execute(action="scan", folder=str(folder))
    assert "error" not in result
    assert len(result["files"]) == 3


def test_scan_filters_by_extension(tmp_path, tool):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"%PDF")
    (folder / "b.txt").write_text("hello")

    result = tool.execute(action="scan", folder=str(folder), extensions=[".pdf"])
    assert len(result["files"]) == 1
    assert result["files"][0]["ext"] == ".pdf"


def test_scan_nonexistent_folder(tool):
    result = tool.execute(action="scan", folder="/nonexistent/path")
    assert "error" in result


def test_scan_missing_folder_param(tool):
    result = tool.execute(action="scan")
    assert "error" in result


# ── copy ─────────────────────────────────────────────────────────────────────

def test_copy_creates_file_in_output_root(tmp_path, tool):
    src = tmp_path / "source.pdf"
    src.write_bytes(b"%PDF-content")

    result = tool.execute(action="copy", src=str(src), dest="docs/source.pdf")
    assert "error" not in result
    assert result["copied"] is True
    assert Path(result["dest_path"]).exists()
    assert Path(result["dest_path"]).read_bytes() == b"%PDF-content"


def test_copy_rejects_path_escape(tmp_path, tool):
    src = tmp_path / "source.pdf"
    src.write_bytes(b"data")

    result = tool.execute(action="copy", src=str(src), dest="../../etc/passwd")
    assert "error" in result
    assert "outside" in result["error"]


def test_copy_missing_src(tool):
    result = tool.execute(action="copy", src="/nonexistent", dest="out.pdf")
    assert "error" in result


def test_copy_missing_params(tool):
    result = tool.execute(action="copy")
    assert "error" in result


# ── mkdir ────────────────────────────────────────────────────────────────────

def test_mkdir_creates_nested_directory(tmp_path, tool):
    result = tool.execute(action="mkdir", path="category/sub")
    assert "error" not in result
    assert result["created"] is True
    assert Path(result["path"]).is_dir()


def test_mkdir_rejects_path_escape(tool):
    result = tool.execute(action="mkdir", path="../../escape")
    assert "error" in result
    assert "outside" in result["error"]


def test_mkdir_missing_path(tool):
    result = tool.execute(action="mkdir")
    assert "error" in result


# ── write_csv ────────────────────────────────────────────────────────────────

def test_write_csv_creates_file(tmp_path, tool):
    result = tool.execute(
        action="write_csv",
        path="results/duplicates.csv",
        headers=["file_a", "file_b", "confidence", "reasoning"],
        rows=[
            ["doc1.pdf", "doc2.pdf", "0.95", "Nearly identical"],
            ["doc3.pdf", "doc4.pdf", "0.82", "Same parties, minor diffs"],
        ],
    )
    assert "error" not in result
    assert result["written"] is True
    assert result["row_count"] == 2

    csv_path = Path(result["path"])
    assert csv_path.exists()

    with open(csv_path) as f:
        reader = csv.reader(f)
        rows = list(reader)
    assert rows[0] == ["file_a", "file_b", "confidence", "reasoning"]
    assert rows[1][0] == "doc1.pdf"
    assert len(rows) == 3  # header + 2 data rows


def test_write_csv_rejects_path_escape(tool):
    result = tool.execute(
        action="write_csv",
        path="../../evil.csv",
        headers=["a"],
        rows=[["b"]],
    )
    assert "error" in result


def test_write_csv_missing_headers(tool):
    result = tool.execute(action="write_csv", path="out.csv", rows=[["a"]])
    assert "error" in result


# ── unknown action ───────────────────────────────────────────────────────────

def test_unknown_action(tool):
    result = tool.execute(action="delete")
    assert "error" in result
    assert "Unknown action" in result["error"]
