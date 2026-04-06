"""In-memory metrics collector for agent observability.

Tracks per-agent counters (ticks, errors, starts, stops), timing
data (tick latency), run counts, and last-run timestamps.
Optionally persists to SQLite via MetricsStore for time-series charts.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class _AgentStats:
    """Mutable counters for a single agent."""

    starts: int = 0
    stops: int = 0
    ticks: int = 0
    errors: int = 0
    started_at: float | None = None
    stopped_at: float | None = None
    last_run_at: float | None = None
    run_count_total: int = 0
    run_count_today: int = 0
    today_date: str = ""
    # Keep last N tick durations for latency percentiles
    tick_durations: list[float] = field(default_factory=list)
    _max_durations: int = 500


class MetricsCollector:
    """Thread-safe, singleton metrics store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, _AgentStats] = defaultdict(_AgentStats)
        self._boot_time = time.time()
        self._store = None  # Optional MetricsStore for persistence
        self._persist_counter: dict[str, int] = defaultdict(int)

    def set_store(self, store) -> None:
        """Attach a MetricsStore for persistent time-series data."""
        self._store = store

    # ── Recording ────────────────────────────────────────────────────

    def record_start(self, agent_name: str) -> None:
        with self._lock:
            s = self._agents[agent_name]
            s.starts += 1
            s.started_at = time.time()
            s.stopped_at = None

    def record_stop(self, agent_name: str) -> None:
        with self._lock:
            s = self._agents[agent_name]
            s.stops += 1
            s.stopped_at = time.time()

    def record_tick(self, agent_name: str, duration_s: float) -> None:
        with self._lock:
            s = self._agents[agent_name]
            s.ticks += 1
            s.tick_durations.append(duration_s)
            if len(s.tick_durations) > s._max_durations:
                s.tick_durations = s.tick_durations[-s._max_durations:]

            # Track run counts and last run
            s.last_run_at = time.time()
            s.run_count_total += 1
            today = time.strftime("%Y-%m-%d")
            if s.today_date != today:
                s.today_date = today
                s.run_count_today = 0
            s.run_count_today += 1

            # Persist every 10 ticks to avoid hammering SQLite
            self._persist_counter[agent_name] += 1
            if self._store and self._persist_counter[agent_name] >= 10:
                self._persist_counter[agent_name] = 0
                running = s.started_at is not None and s.stopped_at is None
                try:
                    self._store.record(
                        agent=agent_name,
                        ticks=10,
                        errors=s.errors,
                        latency_ms=round(duration_s * 1000, 1),
                        status="running" if running else "idle",
                    )
                except Exception:
                    pass  # Never let persistence failure break the agent

    def record_error(self, agent_name: str) -> None:
        with self._lock:
            s = self._agents[agent_name]
            s.errors += 1

    # ── Querying ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of all metrics."""
        now = time.time()
        with self._lock:
            agents = {}
            total_ticks = 0
            total_errors = 0

            for name, s in self._agents.items():
                if s.started_at and not s.stopped_at:
                    uptime = now - s.started_at
                    running = True
                elif s.started_at and s.stopped_at:
                    uptime = s.stopped_at - s.started_at
                    running = False
                else:
                    uptime = 0
                    running = False

                durations = s.tick_durations
                if durations:
                    latency = {
                        "mean_ms": round(statistics.mean(durations) * 1000, 1),
                        "p50_ms": round(statistics.median(durations) * 1000, 1),
                        "p95_ms": round(_percentile(durations, 0.95) * 1000, 1),
                        "p99_ms": round(_percentile(durations, 0.99) * 1000, 1),
                        "min_ms": round(min(durations) * 1000, 1),
                        "max_ms": round(max(durations) * 1000, 1),
                    }
                else:
                    latency = None

                error_rate = (s.errors / s.ticks * 100) if s.ticks > 0 else 0

                agents[name] = {
                    "starts": s.starts,
                    "stops": s.stops,
                    "ticks": s.ticks,
                    "errors": s.errors,
                    "error_rate_pct": round(error_rate, 2),
                    "uptime_s": round(uptime, 1),
                    "running": running,
                    "latency": latency,
                    "last_run_at": s.last_run_at,
                    "run_count_today": s.run_count_today,
                    "run_count_total": s.run_count_total,
                }

                total_ticks += s.ticks
                total_errors += s.errors

            return {
                "server_uptime_s": round(now - self._boot_time, 1),
                "total_agents_tracked": len(agents),
                "total_ticks": total_ticks,
                "total_errors": total_errors,
                "agents": agents,
            }

    def reset(self) -> None:
        """Clear all metrics (mainly for testing)."""
        with self._lock:
            self._agents.clear()


def _percentile(data: list[float], p: float) -> float:
    """Simple percentile without numpy."""
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


# Global singleton
metrics = MetricsCollector()
