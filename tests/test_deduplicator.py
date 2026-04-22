"""Tests for the deduplicator agents."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from secure_agents.core.base_provider import CompletionResponse
from secure_agents.core.job_queue import JobQueue
from secure_agents.agents.deduplicator.agent import (
    NDADeduplicator,
    _jaccard,
    _word_set,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_provider(is_similar: bool = True, confidence: float = 0.90):
    provider = MagicMock()
    provider.complete.return_value = CompletionResponse(
        content=json.dumps({
            "is_similar": is_similar,
            "confidence": confidence,
            "reasoning": "Test comparison result",
        }),
        model="test",
    )
    return provider


def _make_text_extractor(texts: dict[str, str]):
    """Mock text_extractor that returns predefined texts keyed by filename."""
    ext = MagicMock()
    def _execute(**kwargs):
        fpath = kwargs.get("file_path", "")
        fname = Path(fpath).name
        if fname in texts:
            return {
                "text": texts[fname],
                "file_type": "pdf",
                "filename": fname,
                "size_bytes": len(texts[fname]),
            }
        return {"error": f"File not found: {fname}"}
    ext.execute.side_effect = _execute
    return ext


def _make_file_manager(output_root: Path):
    """Mock file_manager that writes real CSV files."""
    mgr = MagicMock()
    def _execute(**kwargs):
        action = kwargs.get("action", "")
        if action == "write_csv":
            path = kwargs.get("path", "")
            headers = kwargs.get("headers", [])
            rows = kwargs.get("rows", [])
            target = output_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            return {"written": True, "path": str(target), "row_count": len(rows)}
        return {"error": f"Unknown action: {action}"}
    mgr.execute.side_effect = _execute
    return mgr


def _setup_dedup(tmp_path, texts, provider=None, is_similar=True, confidence=0.90):
    """Wire up an NDADeduplicator with mock tools and a real job queue."""
    output_root = tmp_path / "ai_generated"
    folder = output_root / "Non-disclosure agreements"
    folder.mkdir(parents=True)

    # Write actual files so the agent can find them
    for name in texts:
        (folder / name).write_bytes(b"fake-content")

    queue = JobQueue(db_path=str(tmp_path / "jobs.db"))
    prov = provider or _mock_provider(is_similar, confidence)

    tools = {
        "text_extractor": _make_text_extractor(texts),
        "file_manager": _make_file_manager(output_root),
    }

    agent = NDADeduplicator(
        tools=tools,
        provider=prov,
        config={"output_root": str(output_root)},
        job_queue=queue,
    )

    # Enqueue a job as if the sorter emitted it
    queue.enqueue(agent="nda_deduplicator", payload={
        "category": "nda",
        "folder_name": "Non-disclosure agreements",
        "output_root": str(output_root),
        "files": list(texts.keys()),
    })

    return agent, queue, output_root


# ── Jaccard pre-filter unit tests ────────────────────────────────────────────

class TestJaccardPrefilter:

    def test_identical_texts_score_one(self):
        a = _word_set("The quick brown fox jumps over the lazy dog")
        b = _word_set("The quick brown fox jumps over the lazy dog")
        assert _jaccard(a, b) == 1.0

    def test_completely_different_texts_score_zero(self):
        a = _word_set("alpha beta gamma delta")
        b = _word_set("epsilon zeta eta theta")
        assert _jaccard(a, b) == 0.0

    def test_partial_overlap(self):
        a = _word_set("contract agreement parties obligations")
        b = _word_set("contract agreement termination renewal")
        sim = _jaccard(a, b)
        assert 0.0 < sim < 1.0

    def test_stop_words_removed(self):
        words = _word_set("the and is a an of")
        assert len(words) == 0

    def test_empty_texts(self):
        assert _jaccard(set(), set()) == 1.0
        assert _jaccard({"word"}, set()) == 0.0


# ── Dedup agent tests ────────────────────────────────────────────────────────

class TestDeduplicator:

    def test_finds_similar_pair(self, tmp_path):
        # Two near-identical texts — must share ≥ 0.95 Jaccard to pass the
        # pre-filter and reach the LLM.  Both texts use the same long legal
        # boilerplate so Jaccard ≈ 0.98; the second adds only one unique word.
        _base = (
            "mutual nondisclosure agreement executed alpha corporation organized "
            "delaware principal place business beta corporation organized california "
            "parties desire engage discussions potential business collaboration "
            "sharing confidential proprietary information facilitate discussions "
            "confidential information means data materials trade secrets technical "
            "financial commercial legal operational strategic affairs disclosed "
            "directly indirectly writing orally electronic form receiving party "
            "agrees hold strict confidence degree care reasonable shall use solely "
            "evaluating potential relationship shall disclose third party prior "
            "written consent disclosing party obligations remain force period five "
            "years date execution governed construed accordance state delaware "
            "conflict principles entire agreement subject matter hereof supersedes "
            "prior agreements understandings thereto representations warranties "
            "indemnification limitation liability termination provisions survive"
        )
        texts = {
            "nda_v1.pdf": _base,
            "nda_v2.pdf": _base + " addendum",
        }
        agent, queue, output_root = _setup_dedup(tmp_path, texts, is_similar=True, confidence=0.92)
        agent.tick()

        # CSV should have been written
        csv_path = output_root / "Non-disclosure agreements" / "duplicates.csv"
        assert csv_path.exists()

        with open(csv_path) as f:
            rows = list(csv.reader(f))
        # Header + 1 similar pair
        assert len(rows) == 2
        assert rows[1][0] in ("nda_v1.pdf", "nda_v2.pdf")
        assert rows[1][2] == "0.92"

    def test_dissimilar_pair_not_in_csv(self, tmp_path):
        # Two completely different texts
        texts = {
            "nda_alpha.pdf": "alpha beta gamma delta epsilon",
            "nda_zeta.pdf":  "zeta eta theta iota kappa",
        }
        provider = _mock_provider(is_similar=False, confidence=0.1)
        agent, queue, output_root = _setup_dedup(tmp_path, texts, provider=provider)
        agent.tick()

        csv_path = output_root / "Non-disclosure agreements" / "duplicates.csv"
        assert csv_path.exists()

        with open(csv_path) as f:
            rows = list(csv.reader(f))
        # Header only — no similar pairs (or Jaccard pre-filter skipped them entirely)
        assert len(rows) == 1  # just header

    def test_empty_folder_writes_empty_csv(self, tmp_path):
        agent, queue, output_root = _setup_dedup(tmp_path, {})
        agent.tick()

        csv_path = output_root / "Non-disclosure agreements" / "duplicates.csv"
        assert csv_path.exists()

        with open(csv_path) as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # header only

    def test_single_file_no_comparison(self, tmp_path):
        texts = {"only_one.pdf": "Some NDA content here about confidential information."}
        provider = _mock_provider()
        agent, queue, output_root = _setup_dedup(tmp_path, texts, provider=provider)
        agent.tick()

        # No pairs to compare → LLM should not be called
        assert provider.complete.call_count == 0

    def test_job_marked_complete(self, tmp_path):
        texts = {"a.pdf": "content"}
        agent, queue, output_root = _setup_dedup(tmp_path, texts)
        agent.tick()

        # Job should be completed (dequeued then completed)
        stats = queue.get_stats("nda_deduplicator")
        assert stats.get("completed", 0) == 1

    def test_no_job_waits_and_returns(self, tmp_path):
        """If no job is in the queue, the agent waits briefly then returns."""
        queue = JobQueue(db_path=str(tmp_path / "jobs.db"))
        provider = _mock_provider()
        agent = NDADeduplicator(
            tools={"text_extractor": MagicMock(), "file_manager": MagicMock()},
            provider=provider,
            config={"output_root": str(tmp_path)},
            job_queue=queue,
        )
        # Tick with no job — should not crash, should not stop permanently
        # (it waits 5s in real life, but _stop_event.wait returns immediately
        # if the event is already set or if timeout=0)
        agent._stop_event.wait = MagicMock()  # don't actually sleep
        agent.tick()
        assert provider.complete.call_count == 0


class TestDeduplicatorErrorHandling:

    def test_extraction_failure_skipped(self, tmp_path):
        texts = {
            "good.pdf": "Non-Disclosure Agreement content here",
            "bad.pdf": "WILL_ERROR",  # we'll override to return error
        }
        provider = _mock_provider()
        agent, queue, output_root = _setup_dedup(tmp_path, texts, provider=provider)

        # Override text_extractor to fail on bad.pdf
        original = agent.tools["text_extractor"].execute.side_effect
        def patched(**kwargs):
            if "bad.pdf" in kwargs.get("file_path", ""):
                return {"error": "corrupt file"}
            return original(**kwargs)
        agent.tools["text_extractor"].execute.side_effect = patched

        agent.tick()
        # Should complete without crashing — only 1 file extracted, so 0 pairs
        assert agent._stop_event.is_set()

    def test_llm_error_skips_pair(self, tmp_path):
        # Two similar texts but LLM will error
        texts = {
            "nda_a.pdf": "Non-Disclosure Agreement between Alpha Corp and Beta Corp "
                         "regarding confidential information protection obligations.",
            "nda_b.pdf": "Non-Disclosure Agreement between Alpha Corp and Beta Corp "
                         "regarding confidential information protection obligations. Updated.",
        }
        provider = MagicMock()
        provider.complete.side_effect = RuntimeError("LLM down")
        agent, queue, output_root = _setup_dedup(tmp_path, texts, provider=provider)
        agent.tick()

        # Should not crash, CSV should exist (empty)
        csv_path = output_root / "Non-disclosure agreements" / "duplicates.csv"
        assert csv_path.exists()
