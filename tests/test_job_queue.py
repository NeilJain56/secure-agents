"""Tests for the SQLite-backed job queue."""

import tempfile
from pathlib import Path

from secure_agents.core.job_queue import JobQueue, JobStatus


def _make_queue(tmp_path: Path) -> JobQueue:
    return JobQueue(db_path=str(tmp_path / "test_jobs.db"), max_retries=3, retry_delay=0)


def test_enqueue_and_dequeue():
    with tempfile.TemporaryDirectory() as tmp:
        q = _make_queue(Path(tmp))
        job = q.enqueue("test_agent", {"file": "test.pdf"})
        assert job.status == JobStatus.PENDING

        dequeued = q.dequeue("test_agent")
        assert dequeued is not None
        assert dequeued.id == job.id
        assert dequeued.status == JobStatus.PROCESSING


def test_dequeue_empty():
    with tempfile.TemporaryDirectory() as tmp:
        q = _make_queue(Path(tmp))
        assert q.dequeue("test_agent") is None


def test_complete_job():
    with tempfile.TemporaryDirectory() as tmp:
        q = _make_queue(Path(tmp))
        job = q.enqueue("test_agent", {})
        q.dequeue("test_agent")
        q.complete(job.id)

        stats = q.get_stats("test_agent")
        assert stats.get("completed") == 1


def test_fail_and_retry():
    with tempfile.TemporaryDirectory() as tmp:
        q = _make_queue(Path(tmp))
        job = q.enqueue("test_agent", {})
        q.dequeue("test_agent")

        # First failure - should retry (re-enqueue as pending)
        q.fail(job.id, "something broke")
        stats = q.get_stats("test_agent")
        assert stats.get("pending") == 1

        # Exhaust retries — job should be moved to dead-letter queue
        for i in range(2):
            q.dequeue("test_agent")
            q.fail(job.id, f"failure {i+2}")

        # Job is removed from the main queue and moved to DLQ
        stats = q.get_stats("test_agent")
        assert stats.get("pending", 0) == 0
        assert q.dlq_count("test_agent") == 1

        # Verify DLQ entry contents
        dlq_entries = q.list_dlq("test_agent")
        assert len(dlq_entries) == 1
        assert dlq_entries[0]["agent"] == "test_agent"
        assert dlq_entries[0]["error"] == "failure 3"

        # Test retry from DLQ
        new_job = q.retry_from_dlq(dlq_entries[0]["id"])
        assert new_job is not None
        assert new_job.status == JobStatus.PENDING
        stats = q.get_stats("test_agent")
        assert stats.get("pending") == 1


def test_stats():
    with tempfile.TemporaryDirectory() as tmp:
        q = _make_queue(Path(tmp))
        q.enqueue("agent_a", {})
        q.enqueue("agent_a", {})
        q.enqueue("agent_b", {})

        stats_a = q.get_stats("agent_a")
        stats_all = q.get_stats()

        assert stats_a.get("pending") == 2
        assert stats_all.get("pending") == 3
