"""SQLite-backed job queue for document processing.

Zero external dependencies - no Redis or RabbitMQ needed.
Supports retry with exponential backoff, parallel workers, and job status tracking.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Generator

import structlog

logger = structlog.get_logger()


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    agent: str
    payload: dict = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    retries: int = 0
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_row(self) -> tuple:
        return (
            self.id,
            self.agent,
            json.dumps(self.payload),
            self.status.value,
            self.retries,
            self.error,
            self.created_at,
            self.updated_at,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            agent=row["agent"],
            payload=json.loads(row["payload"]),
            status=JobStatus(row["status"]),
            retries=row["retries"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class JobQueue:
    """SQLite-backed persistent job queue."""

    def __init__(self, db_path: str = "./data/jobs.db", max_retries: int = 3, retry_delay: int = 60) -> None:
        self.db_path = Path(db_path)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Set restrictive file permissions (owner-only read/write)
        if not self.db_path.exists():
            self.db_path.touch(mode=0o600)
        else:
            self.db_path.chmod(0o600)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    retries INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_agent ON jobs(agent)")
            # Dead-letter queue for permanently failed jobs
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter_jobs (
                    id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    retries INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL,
                    failed_at REAL NOT NULL,
                    retried_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dlq_agent ON dead_letter_jobs(agent)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dlq_failed ON dead_letter_jobs(failed_at)")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, agent: str, payload: dict | None = None) -> Job:
        """Add a new job to the queue."""
        now = time.time()
        job = Job(
            id=str(uuid.uuid4()),
            agent=agent,
            payload=payload or {},
            status=JobStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, agent, payload, status, retries, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                job.to_row(),
            )
        logger.info("job.enqueued", job_id=job.id, agent=agent)
        return job

    def dequeue(self, agent: str) -> Job | None:
        """Get the next pending job for an agent. Returns None if queue is empty."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE agent = ? AND status = ? ORDER BY created_at ASC LIMIT 1",
                (agent, JobStatus.PENDING.value),
            ).fetchone()
            if row is None:
                return None
            job = Job.from_row(row)
            job.status = JobStatus.PROCESSING
            job.updated_at = time.time()
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (job.status.value, job.updated_at, job.id),
            )
        logger.info("job.dequeued", job_id=job.id, agent=agent)
        return job

    def complete(self, job_id: str) -> None:
        """Mark a job as completed."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (JobStatus.COMPLETED.value, time.time(), job_id),
            )
        logger.info("job.completed", job_id=job_id)

    def fail(self, job_id: str, error: str) -> None:
        """Mark a job as failed. Re-enqueues if retries remain."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            retries = row["retries"] + 1
            if retries < self.max_retries:
                conn.execute(
                    "UPDATE jobs SET status = ?, retries = ?, error = ?, updated_at = ? WHERE id = ?",
                    (JobStatus.PENDING.value, retries, error, time.time(), job_id),
                )
                logger.warning("job.retry", job_id=job_id, retries=retries, error=error)
            else:
                # Move to dead-letter queue
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO dead_letter_jobs "
                    "(id, agent, payload, retries, error, created_at, failed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], row["agent"], row["payload"], retries, error,
                     row["created_at"], now),
                )
                conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                logger.error("job.moved_to_dlq", job_id=job_id, error=error)

    def list_dlq(self, agent: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """List dead-letter queue entries."""
        with self._connect() as conn:
            if agent:
                rows = conn.execute(
                    "SELECT * FROM dead_letter_jobs WHERE agent = ? "
                    "ORDER BY failed_at DESC LIMIT ? OFFSET ?",
                    (agent, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM dead_letter_jobs ORDER BY failed_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [
            {
                "id": r["id"], "agent": r["agent"],
                "payload": json.loads(r["payload"]),
                "retries": r["retries"], "error": r["error"],
                "created_at": r["created_at"], "failed_at": r["failed_at"],
                "retried_at": r["retried_at"],
            }
            for r in rows
        ]

    def dlq_count(self, agent: str | None = None) -> int:
        """Count dead-letter queue entries."""
        with self._connect() as conn:
            if agent:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM dead_letter_jobs WHERE agent = ?",
                    (agent,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) as cnt FROM dead_letter_jobs").fetchone()
        return row["cnt"] if row else 0

    def retry_from_dlq(self, job_id: str) -> Job | None:
        """Re-enqueue a dead-letter job for another attempt."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM dead_letter_jobs WHERE id = ?", (job_id,),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            # Create new job in the main queue
            new_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO jobs (id, agent, payload, status, retries, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id, row["agent"], row["payload"], JobStatus.PENDING.value,
                 0, None, now, now),
            )
            # Mark DLQ entry as retried
            conn.execute(
                "UPDATE dead_letter_jobs SET retried_at = ? WHERE id = ?",
                (now, job_id),
            )
        logger.info("job.retried_from_dlq", original_id=job_id, new_id=new_id)
        return Job(
            id=new_id, agent=row["agent"],
            payload=json.loads(row["payload"]),
            status=JobStatus.PENDING, created_at=now, updated_at=now,
        )

    def get_stats(self, agent: str | None = None) -> dict[str, int]:
        """Get job count by status."""
        with self._connect() as conn:
            if agent:
                rows = conn.execute(
                    "SELECT status, COUNT(*) as count FROM jobs WHERE agent = ? GROUP BY status",
                    (agent,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
                ).fetchall()
        return {row["status"]: row["count"] for row in rows}
