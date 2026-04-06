"""Tests for the tool system."""

import json
import tempfile
from pathlib import Path

from secure_agents.core.base_tool import BaseTool
from secure_agents.tools.file_storage import FileStorageTool


def test_base_tool_interface():
    class TestTool(BaseTool):
        name = "test"
        description = "A test tool"
        def execute(self, **kwargs):
            return {"echo": kwargs.get("input", "")}

    tool = TestTool(config={"key": "value"})
    assert tool.name == "test"
    assert tool.config["key"] == "value"
    result = tool.execute(input="hello")
    assert result == {"echo": "hello"}


def test_file_storage_save_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FileStorageTool(config={"output_dir": tmp})

        # Save
        result = tool.execute(action="save", filename="test.json", data={"key": "value"})
        assert result["saved"] is True

        # Load
        result = tool.execute(action="load", filename="test.json")
        assert result["data"]["key"] == "value"


def test_file_storage_list():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FileStorageTool(config={"output_dir": tmp})
        tool.execute(action="save", filename="a.json", data={"a": 1})
        tool.execute(action="save", filename="b.json", data={"b": 2})

        result = tool.execute(action="list")
        assert len(result["files"]) == 2


def test_file_storage_subfolder():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FileStorageTool(config={"output_dir": tmp})
        tool.execute(action="save", filename="report.json", data={"x": 1}, subfolder="reports")

        result = tool.execute(action="load", filename="report.json", subfolder="reports")
        assert result["data"]["x"] == 1


def test_file_storage_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FileStorageTool(config={"output_dir": tmp})
        result = tool.execute(action="load", filename="nonexistent.json")
        assert "error" in result
