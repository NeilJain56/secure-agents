"""Tests for the in-memory metrics collector."""

from secure_agents.core.metrics import MetricsCollector


def test_record_start_and_stop():
    m = MetricsCollector()
    m.record_start("agent_a")
    snap = m.snapshot()
    assert snap["agents"]["agent_a"]["starts"] == 1
    assert snap["agents"]["agent_a"]["running"] is True

    m.record_stop("agent_a")
    snap = m.snapshot()
    assert snap["agents"]["agent_a"]["stops"] == 1
    assert snap["agents"]["agent_a"]["running"] is False
    assert snap["agents"]["agent_a"]["uptime_s"] >= 0


def test_record_ticks_and_latency():
    m = MetricsCollector()
    m.record_start("agent_b")
    for dur in [0.1, 0.2, 0.15, 0.3, 0.05]:
        m.record_tick("agent_b", dur)

    snap = m.snapshot()
    a = snap["agents"]["agent_b"]
    assert a["ticks"] == 5
    assert a["latency"] is not None
    assert a["latency"]["min_ms"] == 50.0
    assert a["latency"]["max_ms"] == 300.0
    assert a["latency"]["p50_ms"] > 0


def test_record_errors():
    m = MetricsCollector()
    m.record_tick("agent_c", 0.1)
    m.record_tick("agent_c", 0.1)
    m.record_error("agent_c")
    snap = m.snapshot()
    a = snap["agents"]["agent_c"]
    assert a["errors"] == 1
    assert a["error_rate_pct"] == 50.0


def test_snapshot_totals():
    m = MetricsCollector()
    m.record_tick("a1", 0.1)
    m.record_tick("a1", 0.1)
    m.record_tick("a2", 0.2)
    m.record_error("a2")
    snap = m.snapshot()
    assert snap["total_ticks"] == 3
    assert snap["total_errors"] == 1
    assert snap["total_agents_tracked"] == 2


def test_reset():
    m = MetricsCollector()
    m.record_start("x")
    m.record_tick("x", 0.1)
    m.reset()
    snap = m.snapshot()
    assert snap["total_agents_tracked"] == 0
    assert snap["agents"] == {}
