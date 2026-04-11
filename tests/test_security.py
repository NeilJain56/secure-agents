"""Tests for security utilities: file validation, filename safety, path containment.

Prompt-injection defense is now a three-layer architecture (schemas + validator
LLM + message boundaries); see ``test_schemas.py``, ``test_validator.py``, and
``test_message_builder.py`` for those tests.  This file covers the file-level
security primitives only.
"""

from pathlib import Path

from secure_agents.core.security import (
    validate_file,
    sanitize_filename,
    validate_path_within,
)


# ── File validation ──────────────────────────────────────────────────────────

def test_validate_file_valid_pdf(tmp_path):
    f = tmp_path / "test.pdf"
    f.write_bytes(b"%PDF-1.4 fake content")
    ok, reason = validate_file(f)
    assert ok is True
    assert reason == "OK"


def test_validate_file_valid_docx(tmp_path):
    f = tmp_path / "test.docx"
    # DOCX is a ZIP — starts with PK
    f.write_bytes(b"PK\x03\x04 fake content")
    ok, reason = validate_file(f)
    assert ok is True


def test_validate_file_wrong_magic_bytes(tmp_path):
    """A .pdf file that doesn't start with %PDF is rejected."""
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"MZ\x90\x00 this is an exe")
    ok, reason = validate_file(f)
    assert ok is False
    assert "does not match" in reason


def test_validate_file_disallowed_type(tmp_path):
    f = tmp_path / "test.exe"
    f.write_bytes(b"MZ content")
    ok, reason = validate_file(f)
    assert ok is False
    assert "not allowed" in reason


def test_validate_file_too_large(tmp_path):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"%PDF" + b"x" * (2 * 1024 * 1024))
    ok, reason = validate_file(f, max_size_mb=1)
    assert ok is False
    assert "too large" in reason


def test_validate_file_path_traversal(tmp_path):
    f = tmp_path / ".." / "etc" / "passwd.pdf"
    ok, reason = validate_file(f)
    assert ok is False


def test_validate_file_nonexistent():
    ok, reason = validate_file("/nonexistent/file.pdf")
    assert ok is False


# ── Filename sanitization ────────────────────────────────────────────────────

def test_sanitize_filename_basic():
    assert sanitize_filename("report.json") == "report.json"


def test_sanitize_filename_strips_dangerous_chars():
    assert sanitize_filename("../../etc/passwd") == "etcpasswd"


def test_sanitize_filename_strips_spaces_and_special():
    result = sanitize_filename("my file (1).pdf")
    assert " " not in result
    assert "(" not in result


def test_sanitize_filename_max_length():
    long_name = "a" * 300 + ".pdf"
    result = sanitize_filename(long_name, max_length=100)
    assert len(result) <= 100


def test_sanitize_filename_empty_returns_unnamed():
    assert sanitize_filename("") == "unnamed"
    assert sanitize_filename("///") == "unnamed"


def test_sanitize_filename_no_leading_dots():
    result = sanitize_filename(".hidden")
    assert not result.startswith(".")


# ── Path containment ────────────────────────────────────────────────────────

def test_validate_path_within_safe():
    root = Path("/tmp/output")
    path = Path("/tmp/output/reports/file.json")
    assert validate_path_within(path, root) is True


def test_validate_path_within_traversal():
    root = Path("/tmp/output")
    path = Path("/tmp/output/../../etc/passwd")
    assert validate_path_within(path, root) is False


def test_validate_path_within_absolute_escape():
    root = Path("/tmp/output")
    path = Path("/etc/passwd")
    assert validate_path_within(path, root) is False
