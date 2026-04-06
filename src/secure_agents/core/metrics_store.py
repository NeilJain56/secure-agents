"""SQLite-backed persistent time-series metrics storage.

Complements the in-memory MetricsCollector by durably recording per-agent
metrics to a local SQLite database.  Supports raw queries, hourly rollups,
status distribution, and CSV export.

Zero external dependencies beyond the standard library and structlog.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_store_lock = threading.Lock()
_store_instance: "MetricsStore | None" = None


class MetricsStore:
    """SQLite-backed persistent time-series metrics store."""

    def __init__(self, db_path: str = "./data/metrics.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    # ── DB helpers ───────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics_ts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    agent TEXT NOT NULL,
                    ticks INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    latency_ms REAL,
                    status TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_agent_ts ON metrics_ts(agent, ts)"
            )
        logger.debug("metrics_store.initialized", db_path=str(self.db_path))

    # ── Recording ────────────────────────────────────────────────────

    def record(
        self,
        agent: str,
        ticks: int,
        errors: int,
        latency_ms: float | None,
        status: str = "running",
    ) -> None:
        """Append a metrics data point."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metrics_ts (ts, agent, ticks, errors, latency_ms, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, agent, ticks, errors, latency_ms, status),
            )
        logger.debug(
            "metrics_store.recorded",
            agent=agent,
            ticks=ticks,
            errors=errors,
            latency_ms=latency_ms,
            status=status,
        )

    # ── Querying ─────────────────────────────────────────────────────

    def query(
        self, agent: str | None = None, range_hours: int = 24
    ) -> list[dict]:
        """Get raw data points within a time range."""
        cutoff = time.time() - range_hours * 3600
        with self._connect() as conn:
            if agent:
                rows = conn.execute(
                    "SELECT * FROM metrics_ts WHERE agent = ? AND ts >= ? ORDER BY ts ASC",
                    (agent, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM metrics_ts WHERE ts >= ? ORDER BY ts ASC",
                    (cutoff,),
                ).fetchall()
        return [dict(row) for row in rows]

    def query_hourly(
        self, agent: str | None = None, range_hours: int = 168
    ) -> list[dict]:
        """Hourly rollup: SUM ticks, SUM errors, AVG latency grouped by hour."""
        cutoff = time.time() - range_hours * 3600
        # Cast ts to integer hour bucket (seconds since epoch // 3600)
        base_sql = """
            SELECT
                CAST(ts / 3600 AS INTEGER) * 3600 AS hour_ts,
                agent,
                SUM(ticks)   AS total_ticks,
                SUM(errors)  AS total_errors,
                AVG(latency_ms) AS avg_latency_ms,
                COUNT(*)     AS sample_count
            FROM metrics_ts
            WHERE ts >= ?
        """
        if agent:
            base_sql += " AND agent = ?"
            params: tuple = (cutoff, agent)
        else:
            params = (cutoff,)

        base_sql += " GROUP BY hour_ts, agent ORDER BY hour_ts ASC"

        with self._connect() as conn:
            rows = conn.execute(base_sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_status_distribution(self) -> dict:
        """Latest status counts: how many agents running vs idle.

        Uses the most recent row per agent to determine current status.
        """
        sql = """
            SELECT status, COUNT(*) AS count
            FROM (
                SELECT agent, status
                FROM metrics_ts
                WHERE id IN (
                    SELECT MAX(id) FROM metrics_ts GROUP BY agent
                )
            )
            GROUP BY status
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return {row["status"]: row["count"] for row in rows}

    # ── Export ────────────────────────────────────────────────────────

    def export_csv(
        self, agent: str | None = None, range_hours: int = 24
    ) -> str:
        """Return metrics as a CSV string."""
        rows = self.query(agent=agent, range_hours=range_hours)
        if not rows:
            return ""

        output = io.StringIO()
        fieldnames = ["id", "ts", "agent", "ticks", "errors", "latency_ms", "status"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return output.getvalue()


# ---------------------------------------------------------------------------
# Convenience singleton accessor
# ---------------------------------------------------------------------------

def get_store(db_path: str = "./data/metrics.db") -> MetricsStore:
    """Get or create the singleton metrics store."""
    global _store_instance  # noqa: PLW0603
    if _store_instance is None:
        with _store_lock:
            # Double-checked locking
            if _store_instance is None:
                _store_instance = MetricsStore(db_path=db_path)
    return _store_instance
