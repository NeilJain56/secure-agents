"""Tests for the doc_sorter agent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from secure_agents.core.base_provider import CompletionResponse
from secure_agents.core.job_queue import JobQueue
from secure_agents.agents.doc_sorter.agent import DocSorterAgent
from secure_agents.agents.doc_sorter.prompts import CATEGORY_FOLDERS


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_provider(category: str = "nda", confidence: float = 0.95):
    """Provider that always returns the given category."""
    provider = MagicMock()
    provider.complete.return_value = CompletionResponse(
        content=json.dumps({
            "category": category,
            "confidence": confidence,
            "reasoning": "Test classification",
        }),
        model="test",
    )
    return provider


def _make_text_extractor():
    """Mock text_extractor that returns fake text."""
    ext = MagicMock()
    ext.execute.return_value = {
        "text": "This Non-Disclosure Agreement is entered into...",
        "file_type": "pdf",
        "filename": "test.pdf",
        "size_bytes": 1000,
    }
    return ext


def _make_file_manager(tmp_path: Path):
    """Mock file_manager that tracks calls."""
    mgr = MagicMock()
    mgr.execute.side_effect = _file_manager_side_effect(tmp_path)
    return mgr


def _file_manager_side_effect(tmp_path: Path):
    """Return a callable that mimics file_manager.execute()."""
    def side_effect(**kwargs):
        action = kwargs.get("action", "")
        if action == "mkdir":
            p = tmp_path / "output" / kwargs.get("path", "")
            p.mkdir(parents=True, exist_ok=True)
            return {"created": True, "path": str(p)}
        if action == "scan":
            folder = Path(kwargs.get("folder", ""))
            exts = set(kwargs.get("extensions", []))
            files = []
            if folder.is_dir():
                for f in sorted(folder.iterdir()):
                    if f.is_file() and (not exts or f.suffix.lower() in exts):
                        files.append({
                            "name": f.name,
                            "path": str(f),
                            "size_bytes": f.stat().st_size,
                            "ext": f.suffix.lower(),
                        })
            return {"files": files}
        if action == "copy":
            return {"copied": True, "dest_path": kwargs.get("dest", "")}
        return {"error": f"Unknown action: {action}"}
    return side_effect


def _build_agent(tmp_path, provider, file_list=None, queue=None):
    """Build a DocSorterAgent with mocked tools."""
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    for name in (file_list or ["contract.pdf"]):
        (source / name).write_bytes(b"%PDF-fake-content")

    tools = {
        "text_extractor": _make_text_extractor(),
        "file_manager": _make_file_manager(tmp_path),
    }
    config = {
        "source_folder": str(source),
        "output_root": str(tmp_path / "output"),
    }
    return DocSorterAgent(
        tools=tools,
        provider=provider,
        config=config,
        job_queue=queue,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

class TestDocSorterClassification:

    def test_classifies_single_file(self, tmp_path):
        provider = _mock_provider("nda", 0.92)
        agent = _build_agent(tmp_path, provider)
        agent.tick()

        # LLM should have been called once (one file)
        assert provider.complete.call_count == 1
        # Agent should stop after processing
        assert agent._stop_event.is_set()

    def test_classifies_multiple_files(self, tmp_path):
        provider = _mock_provider("msa_company", 0.88)
        files = ["doc1.pdf", "doc2.pdf", "doc3.pdf"]
        agent = _build_agent(tmp_path, provider, file_list=files)
        agent.tick()

        assert provider.complete.call_count == 3

    def test_copies_file_to_correct_folder(self, tmp_path):
        provider = _mock_provider("msa_thirdparty")
        agent = _build_agent(tmp_path, provider, file_list=["vendor_msa.pdf"])
        agent.tick()

        file_mgr = agent.tools["file_manager"]
        # Find the copy call
        copy_calls = [
            c for c in file_mgr.execute.call_args_list
            if c.kwargs.get("action") == "copy"
        ]
        assert len(copy_calls) == 1
        dest = copy_calls[0].kwargs["dest"]
        assert CATEGORY_FOLDERS["msa_thirdparty"] in dest

    def test_handles_empty_source_folder(self, tmp_path):
        provider = _mock_provider("nda")
        # Build with file_list=None → _build_agent creates source dir but
        # we remove all files so the scan returns nothing.
        source = tmp_path / "empty_source"
        source.mkdir()
        tools = {
            "text_extractor": _make_text_extractor(),
            "file_manager": _make_file_manager(tmp_path),
        }
        agent = DocSorterAgent(
            tools=tools, provider=provider,
            config={"source_folder": str(source), "output_root": str(tmp_path / "output")},
        )
        agent.tick()

        assert provider.complete.call_count == 0
        assert agent._stop_event.is_set()


class TestDocSorterEmit:

    def test_emits_to_three_dedup_agents(self, tmp_path):
        queue = JobQueue(db_path=str(tmp_path / "jobs.db"))
        provider = _mock_provider("nda")
        agent = _build_agent(tmp_path, provider, file_list=["nda.pdf"], queue=queue)
        agent.tick()

        # Should have emitted to all three dedup agents
        nda_job = queue.dequeue("nda_deduplicator")
        company_job = queue.dequeue("msa_company_deduplicator")
        thirdparty_job = queue.dequeue("msa_thirdparty_deduplicator")

        assert nda_job is not None
        assert company_job is not None
        assert thirdparty_job is not None

        # NDA agent should have the file in its list
        assert "nda.pdf" in nda_job.payload["files"]
        # Others should have empty file lists
        assert company_job.payload["files"] == []
        assert thirdparty_job.payload["files"] == []

    def test_emit_payload_contains_folder_name(self, tmp_path):
        queue = JobQueue(db_path=str(tmp_path / "jobs.db"))
        provider = _mock_provider("nda")
        agent = _build_agent(tmp_path, provider, file_list=["test.pdf"], queue=queue)
        agent.tick()

        job = queue.dequeue("nda_deduplicator")
        assert job.payload["folder_name"] == "Non-disclosure agreements"
        assert "output_root" in job.payload


class TestDocSorterErrorHandling:

    def test_missing_source_folder_stops_agent(self, tmp_path):
        provider = _mock_provider("nda")
        tools = {
            "text_extractor": _make_text_extractor(),
            "file_manager": _make_file_manager(tmp_path),
        }
        agent = DocSorterAgent(
            tools=tools, provider=provider,
            config={"source_folder": "", "output_root": str(tmp_path)},
        )
        agent.tick()
        assert agent._stop_event.is_set()
        assert provider.complete.call_count == 0

    def test_extraction_failure_skips_file(self, tmp_path):
        provider = _mock_provider("nda")
        agent = _build_agent(tmp_path, provider, file_list=["bad.pdf"])
        # Make text_extractor return an error
        agent.tools["text_extractor"].execute.return_value = {"error": "corrupt file"}
        agent.tick()

        assert provider.complete.call_count == 0

    def test_llm_error_skips_file(self, tmp_path):
        provider = MagicMock()
        provider.complete.side_effect = RuntimeError("LLM down")
        agent = _build_agent(tmp_path, provider, file_list=["test.pdf"])
        agent.tick()

        # Should not crash, just skip
        assert agent._stop_event.is_set()
