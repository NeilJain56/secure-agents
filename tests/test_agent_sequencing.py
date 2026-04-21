"""Tests for multi-agent sequencing via the shared JobQueue.

Covers:
- Queue wiring (two agents share one queue, A emits to B)
- Sequential handoff (A completes work, emits to B)
- Parallel fan-out (A emits to B and C in one tick)
- Orchestrator pattern (state-machine routing)
- No-op safety (emit without a queue raises nothing)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from secure_agents.core.base_agent import BaseAgent
from secure_agents.core.job_queue import JobQueue, JobStatus


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_queue(tmp_path: Path) -> JobQueue:
    """Create a throw-away queue in *tmp_path*."""
    return JobQueue(db_path=str(tmp_path / "jobs.db"))


class _StubAgent(BaseAgent):
    """Minimal concrete agent for testing.  Records tick calls."""

    name = "stub"
    description = "test stub"

    def __init__(self, *, name: str = "stub", **kwargs):
        # Allow a mock/no-op provider + empty tools
        kwargs.setdefault("tools", {})
        kwargs.setdefault("provider", MagicMock())
        kwargs.setdefault("config", {})
        super().__init__(**kwargs)
        self.name = name
        self.tick_count = 0

    def tick(self) -> None:
        self.tick_count += 1
        self._stop_event.set()


# ── Tests ────────────────────────────────────────────────────────────────────

class TestQueueWiring:
    """Two stub agents share one JobQueue; A emits to B."""

    def test_emit_creates_pending_job_for_target_agent(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent_a = _StubAgent(name="agent_a", job_queue=queue)
        agent_b = _StubAgent(name="agent_b", job_queue=queue)

        agent_a.emit("agent_b", {"document": "contract.pdf"})

        job = queue.dequeue("agent_b")
        assert job is not None
        assert job.agent == "agent_b"
        assert job.payload == {"document": "contract.pdf"}
        assert job.status == JobStatus.PROCESSING  # dequeue marks it processing

        # Nothing left for agent_a
        assert queue.dequeue("agent_a") is None

    def test_both_agents_share_same_queue_instance(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent_a = _StubAgent(name="agent_a", job_queue=queue)
        agent_b = _StubAgent(name="agent_b", job_queue=queue)

        assert agent_a.job_queue is agent_b.job_queue


class TestSequentialHandoff:
    """Agent A completes a job then emits to agent B."""

    def test_handoff_produces_correct_payload(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent_a = _StubAgent(name="agent_a", job_queue=queue)

        # Simulate: A finishes its own work and hands off to B
        result = {"risk_score": 7, "document": "nda.pdf"}
        agent_a.emit("agent_b", result)

        job = queue.dequeue("agent_b")
        assert job is not None
        assert job.payload["risk_score"] == 7
        assert job.payload["document"] == "nda.pdf"

    def test_handoff_after_completing_own_job(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent_a = _StubAgent(name="agent_a", job_queue=queue)

        # Someone enqueued work for A
        own_job = queue.enqueue(agent="agent_a", payload={"step": "review"})
        dequeued = queue.dequeue("agent_a")
        assert dequeued is not None
        queue.complete(dequeued.id)

        # A now emits a follow-up for B
        agent_a.emit("agent_b", {"step": "notify", "source_job": dequeued.id})

        follow_up = queue.dequeue("agent_b")
        assert follow_up is not None
        assert follow_up.payload["step"] == "notify"
        assert follow_up.payload["source_job"] == dequeued.id


class TestParallelFanOut:
    """Agent A emits to both B and C in one tick."""

    def test_fan_out_creates_jobs_for_both_targets(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent_a = _StubAgent(name="agent_a", job_queue=queue)

        agent_a.emit("agent_b", {"task": "summarize"})
        agent_a.emit("agent_c", {"task": "archive"})

        job_b = queue.dequeue("agent_b")
        job_c = queue.dequeue("agent_c")

        assert job_b is not None
        assert job_b.payload["task"] == "summarize"
        assert job_c is not None
        assert job_c.payload["task"] == "archive"

    def test_fan_out_jobs_are_independent(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent_a = _StubAgent(name="agent_a", job_queue=queue)

        agent_a.emit("agent_b", {"doc": "a.pdf"})
        agent_a.emit("agent_c", {"doc": "b.pdf"})

        # Completing B's job does not affect C's
        job_b = queue.dequeue("agent_b")
        assert job_b is not None
        queue.complete(job_b.id)

        job_c = queue.dequeue("agent_c")
        assert job_c is not None
        assert job_c.status == JobStatus.PROCESSING


class TestOrchestratorPattern:
    """A stub orchestrator with no tools routes based on a state flag."""

    def test_orchestrator_routes_by_state(self, tmp_path):
        queue = _make_queue(tmp_path)

        class _Orchestrator(BaseAgent):
            name = "orchestrator"
            description = "routes work"

            def __init__(self, state: str, **kwargs):
                kwargs.setdefault("tools", {})
                kwargs.setdefault("provider", MagicMock())
                kwargs.setdefault("config", {})
                super().__init__(**kwargs)
                self.state = state

            def tick(self) -> None:
                if self.state == "review":
                    self.emit("reviewer", {"action": "review_nda"})
                elif self.state == "notify":
                    self.emit("notifier", {"action": "send_email"})
                else:
                    self.emit("fallback", {"action": "log_unknown"})
                self._stop_event.set()

        # State: review
        orch = _Orchestrator(state="review", job_queue=queue)
        orch.tick()
        job = queue.dequeue("reviewer")
        assert job is not None
        assert job.payload["action"] == "review_nda"
        assert queue.dequeue("notifier") is None

        # State: notify
        orch.state = "notify"
        orch._stop_event.clear()
        orch.tick()
        job = queue.dequeue("notifier")
        assert job is not None
        assert job.payload["action"] == "send_email"

        # State: unknown
        orch.state = "unknown"
        orch._stop_event.clear()
        orch.tick()
        job = queue.dequeue("fallback")
        assert job is not None
        assert job.payload["action"] == "log_unknown"


class TestNoOpSafety:
    """An agent with job_queue=None can call emit() without raising."""

    def test_emit_without_queue_is_silent_noop(self):
        agent = _StubAgent(name="lonely_agent")
        assert agent.job_queue is None

        # Must not raise
        agent.emit("anyone", {"data": "test"})

    def test_emit_without_queue_does_not_create_jobs(self, tmp_path):
        queue = _make_queue(tmp_path)
        agent = _StubAgent(name="lonely_agent")  # no queue wired

        agent.emit("target", {"data": "test"})

        # Nothing appeared in the queue
        assert queue.dequeue("target") is None
