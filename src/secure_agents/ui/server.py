"""FastAPI backend for the Secure Agents dashboard.

Provides a REST API for managing agents: listing, health checks,
setup validation, credential storage, start/stop control, metrics,
outputs, audit log, triggers, and dead-letter queue.

Security:
- Binds to 127.0.0.1 ONLY (never 0.0.0.0)
- Strict CORS: only localhost origins allowed
- Per-session auth token required on all state-changing endpoints
- Error messages are sanitized (no internal details leak to clients)
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import yaml
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from secure_agents.core.agent_status import (
    get_gate,
    get_pipeline_started_at,
    is_pipeline_running,
    is_running_externally,
    write_gate_approval,
)
from secure_agents.core.builder import build_agent, discover_all
from secure_agents.core.config import load_config, AppConfig, validate_agent_name
from secure_agents.core.credentials import get_credential, store_credential, get_oauth2_token
from secure_agents.core.logger import setup_logging
from secure_agents.core.metrics import metrics
from secure_agents.core.registry import registry

logger = structlog.get_logger()

app = FastAPI(title="Secure Agents", docs_url=None, redoc_url=None)

# ── Security: Auth token ────────────────────────────────────────────────────
# Generated once at startup. Required on all state-changing endpoints.
_auth_token: str = ""


def _generate_auth_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


async def _check_auth(request: Request) -> None:
    """Verify the auth token on state-changing requests."""
    if not _auth_token:
        return  # Token not yet initialized (shouldn't happen)
    token = request.headers.get("X-Auth-Token", "")
    if not secrets.compare_digest(token, _auth_token):
        raise HTTPException(403, "Invalid or missing auth token")


# ── Global state ─────────────────────────────────────────────────────────────

_config: AppConfig | None = None
_config_path: str = "config.yaml"
_running_agents: dict[str, dict] = {}  # name -> {"agent": BaseAgent, "thread": Thread}
_worker_semaphore: threading.Semaphore | None = None
_metrics_store = None
_job_queue = None
_trigger_manager = None


def _get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def _reload_config() -> AppConfig:
    global _config
    _config = load_config(_config_path)
    return _config


def _validate_agent_name_param(name: str) -> None:
    """Validate an agent name from a request parameter."""
    if not validate_agent_name(name):
        raise HTTPException(400, "Invalid agent name")


def _safe_error(e: Exception) -> str:
    """Return a safe error message — no internal paths or stack traces."""
    msg = str(e)
    # Strip anything that looks like a file path
    if "/" in msg and ("src/" in msg or "Users/" in msg or "home/" in msg):
        return "An internal error occurred"
    # Cap length to prevent info leakage
    if len(msg) > 200:
        return msg[:200] + "..."
    return msg


# ── Health check logic ───────────────────────────────────────────────────────

def _check_agent_health(agent_name: str, config: AppConfig) -> dict:
    """Run health checks for an agent: provider, credentials, tools."""
    merged = config.get_agent_config(agent_name)
    checks = []

    # 1. Check provider — resolved from config (any local provider allowed)
    agent_provider_cfg = merged.get("provider", {}) or {}
    provider_name = agent_provider_cfg.get("override") or config.active_provider
    try:
        provider_cls = registry.get_provider(provider_name)
        provider_settings = config.get_provider_settings(provider_name)
        provider = provider_cls(provider_settings.model_dump())
        if provider.is_available():
            checks.append({"name": f"Provider ({provider_name})", "status": "ok",
                           "detail": "Connected and ready"})
        else:
            # Ollama has a known auto-start path; other providers must be
            # started manually by the user.
            if provider_name == "ollama" and shutil.which("ollama"):
                _ensure_ollama()
                try:
                    started = provider.is_available()
                except Exception:
                    started = False
                if started:
                    checks.append({"name": f"Provider ({provider_name})", "status": "ok",
                                   "detail": "Connected and ready (auto-started)"})
                else:
                    checks.append({"name": f"Provider ({provider_name})", "status": "error",
                                   "detail": "Ollama is installed but not responding",
                                   "setup_action": "ollama"})
            elif provider_name == "ollama":
                checks.append({"name": f"Provider ({provider_name})", "status": "error",
                               "detail": "Ollama is not installed",
                               "setup_action": "ollama_install"})
            else:
                checks.append({"name": f"Provider ({provider_name})", "status": "error",
                               "detail": f"{provider_name} server is not reachable at {provider_settings.host}"})
    except KeyError:
        checks.append({"name": f"Provider ({provider_name})", "status": "error",
                       "detail": f"Provider '{provider_name}' is not registered"})
    except Exception:
        checks.append({"name": f"Provider ({provider_name})", "status": "error",
                       "detail": "Provider check failed"})

    # 2. Check credentials needed by tools
    tool_names = merged.get("tools", [])
    email_cfg = merged.get("email", {})

    if "email_reader" in tool_names or "email_sender" in tool_names:
        auth_method = email_cfg.get("imap", {}).get("auth_method", "app_password")
        username = email_cfg.get("imap", {}).get("username", "")

        if not username or username == "your-email@gmail.com":
            checks.append({"name": "Email account", "status": "error",
                           "detail": "Email username not configured",
                           "setup_action": "email_config"})
        elif auth_method == "oauth2":
            token = get_oauth2_token(username)
            if token:
                checks.append({"name": "Gmail OAuth2", "status": "ok", "detail": f"Token found for {username}"})
            else:
                checks.append({"name": "Gmail OAuth2", "status": "error",
                               "detail": "No OAuth2 token. Need to authorize with Google.",
                               "setup_action": "oauth2"})
        else:
            pwd = get_credential("email_password")
            if pwd:
                checks.append({"name": "Email password", "status": "ok", "detail": "Found in keychain/env"})
            else:
                checks.append({"name": "Email password", "status": "error",
                               "detail": "No email password found",
                               "setup_action": "credential",
                               "setup_key": "email_password"})

    all_ok = all(c["status"] == "ok" for c in checks)
    return {"healthy": all_ok, "checks": checks}


# ── API Routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    """Serve the single-page dashboard with the auth token embedded."""
    html_path = Path(__file__).parent / "dashboard.html"
    html = html_path.read_text()
    # Inject the auth token as a meta tag so JavaScript can read it
    token_meta = f'<meta name="auth-token" content="{_auth_token}">'
    html = html.replace("<head>", f"<head>\n    {token_meta}", 1)
    return HTMLResponse(html)


@app.get("/api/agents")
def list_agents():
    """List all agents with their config, health, and run status."""
    config = _reload_config()
    agents = []

    for name in registry.list_agents():
        cls = registry.get_agent(name)
        agent_cfg = config.agents.get(name, {})
        merged = config.get_agent_config(name)
        health = _check_agent_health(name, config)
        is_running = (
            (name in _running_agents and _running_agents[name]["thread"].is_alive())
            or is_running_externally(name)
        )

        email_username = merged.get("email", {}).get("imap", {}).get("username", "")
        auth_method = merged.get("email", {}).get("imap", {}).get("auth_method", "app_password")

        # Resolve which provider this agent actually uses
        agent_provider_cfg = merged.get("provider", {}) or {}
        agent_provider_name = agent_provider_cfg.get("override") or config.active_provider

        # Pull run stats from metrics snapshot
        snap = metrics.snapshot()
        agent_metrics = snap.get("agents", {}).get(name, {})

        agents.append({
            "name": name,
            "description": cls.description,
            "features": getattr(cls, "features", []),
            "version": getattr(cls, "version", "0.1.0"),
            "enabled": agent_cfg.get("enabled", True),
            "configured": name in config.agents,
            "running": is_running,
            "health": health,
            "tools": merged.get("tools", []),
            "provider": agent_provider_name,
            "poll_interval": merged.get("poll_interval_seconds", 60),
            "email_username": email_username if email_username != "your-email@gmail.com" else "",
            "email_auth_method": auth_method,
            "available_providers": registry.list_providers(),
            "last_run_at": agent_metrics.get("last_run_at"),
            "run_count_today": agent_metrics.get("run_count_today", 0),
            "run_count_total": agent_metrics.get("run_count_total", 0),
        })

    return {"agents": agents}


@app.get("/api/agents/{agent_name}/health")
def agent_health(agent_name: str):
    """Get detailed health status for a specific agent."""
    _validate_agent_name_param(agent_name)
    config = _reload_config()
    if agent_name not in registry.list_agents():
        raise HTTPException(404, "Agent not found")
    return _check_agent_health(agent_name, config)


class StartRequest(BaseModel):
    agents: list[str]


@app.post("/api/agents/start")
async def start_agents(req: StartRequest, request: Request):
    """Start one or more agents in background threads with concurrency limits."""
    await _check_auth(request)
    config = _reload_config()
    started = []
    errors = []

    # Count currently running agents
    running_count = sum(1 for e in _running_agents.values() if e["thread"].is_alive())

    for name in req.agents:
        _validate_agent_name_param(name)

        if name in _running_agents and _running_agents[name]["thread"].is_alive():
            errors.append({"agent": name, "error": "Already running"})
            continue

        # Enforce global max_workers
        if running_count >= config.max_workers:
            errors.append({"agent": name, "error": f"Max workers ({config.max_workers}) reached"})
            continue

        # Enforce per-agent concurrency_limit
        agent_cfg = config.agents.get(name, {})
        concurrency_limit = agent_cfg.get("concurrency_limit", 1)
        agent_running = sum(
            1 for n, e in _running_agents.items()
            if n == name and e["thread"].is_alive()
        )
        if agent_running >= concurrency_limit:
            errors.append({"agent": name, "error": f"Concurrency limit ({concurrency_limit}) reached"})
            continue

        try:
            agent = build_agent(name, config)

            def _run_agent(a):
                try:
                    a.run()
                finally:
                    # Auto-cleanup from running dict
                    _running_agents.pop(a.name, None)

            t = threading.Thread(target=_run_agent, args=(agent,), name=f"agent-{name}", daemon=True)
            t.start()
            _running_agents[name] = {"agent": agent, "thread": t}
            started.append(name)
            running_count += 1
        except Exception as e:
            errors.append({"agent": name, "error": _safe_error(e)})

    return {"started": started, "errors": errors}


@app.post("/api/agents/{agent_name}/stop")
async def stop_agent(agent_name: str, request: Request):
    """Stop a running agent."""
    await _check_auth(request)
    _validate_agent_name_param(agent_name)
    if agent_name not in _running_agents:
        raise HTTPException(404, "Agent is not running")

    entry = _running_agents.get(agent_name)
    if entry is None:
        raise HTTPException(404, "Agent is not running")
    entry["agent"].request_stop()
    entry["thread"].join(timeout=5.0)
    _running_agents.pop(agent_name, None)
    return {"stopped": agent_name}


@app.post("/api/agents/stop-all")
async def stop_all_agents(request: Request):
    """Stop all running agents."""
    await _check_auth(request)
    stopped = []
    for name, entry in list(_running_agents.items()):
        entry["agent"].request_stop()
        stopped.append(name)
    for name in stopped:
        entry = _running_agents.pop(name, None)
        if entry:
            entry["thread"].join(timeout=5.0)
    return {"stopped": stopped}


class CredentialRequest(BaseModel):
    key: str
    value: str


@app.post("/api/credentials")
async def save_credential(req: CredentialRequest, request: Request):
    """Store a credential in the macOS Keychain."""
    await _check_auth(request)
    if store_credential(req.key, req.value):
        return {"stored": True, "key": req.key}
    raise HTTPException(500, "Failed to store credential")


class TestEmailRequest(BaseModel):
    username: str = ""
    auth_method: str = "app_password"
    host: str = "imap.gmail.com"
    port: int = 993


@app.post("/api/test-email")
async def test_email_connection(req: TestEmailRequest, request: Request):
    """Test IMAP connection with current credentials."""
    await _check_auth(request)
    config = _get_config()

    # Use request params or fall back to config defaults
    username = req.username or config.defaults.get("email", {}).get("imap", {}).get("username", "")
    auth_method = req.auth_method or config.defaults.get("email", {}).get("imap", {}).get("auth_method", "app_password")
    host = req.host or config.defaults.get("email", {}).get("imap", {}).get("host", "imap.gmail.com")
    port = req.port or int(config.defaults.get("email", {}).get("imap", {}).get("port", 993))

    if not username or username == "your-email@gmail.com":
        raise HTTPException(400, "Email username not configured")

    try:
        from imapclient import IMAPClient
    except ImportError:
        raise HTTPException(500, "IMAP client not available")

    try:
        with IMAPClient(host, port=port, ssl=True, timeout=10) as client:
            if auth_method == "oauth2":
                token = get_oauth2_token(username)
                if not token:
                    return {"success": False, "error": "No OAuth2 token found. Run: secure-agents auth gmail client_secrets.json"}
                client.oauth2_login(username, token)
            else:
                password = get_credential("email_password")
                if not password:
                    return {"success": False, "error": "No email password found. Save it using the credential form above."}
                client.login(username, password)

            # If we get here, authentication succeeded
            folders = client.list_folders()
            folder_count = len(folders) if folders else 0
            return {"success": True, "detail": f"Connected to {host} as {username} ({folder_count} folders)"}

    except Exception as e:
        error_msg = str(e)
        if "AUTHENTICATIONFAILED" in error_msg.upper() or "Invalid credentials" in error_msg:
            if auth_method == "oauth2":
                error_msg = "OAuth2 authentication failed. Token may be expired. Re-run: secure-agents auth gmail client_secrets.json"
            else:
                error_msg = "Authentication failed. Check your email and app password."
        elif "Connection refused" in error_msg or "timed out" in error_msg.lower():
            error_msg = f"Could not connect to {host}:{port}. Check your network connection."
        else:
            error_msg = "Email connection test failed"
        return {"success": False, "error": error_msg}


def _update_yaml_value(key_path: str, value: Any) -> None:
    """Update a value in config.yaml while preserving comments and formatting."""
    import re

    path = Path(_config_path)
    if not path.exists():
        path.write_text("")

    content = path.read_text()
    keys = key_path.split(".")
    leaf = keys[-1]

    lines = content.split('\n')
    target_line = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f'{leaf}:'):
            if _line_matches_path(lines, i, keys):
                target_line = i
                break

    if target_line is not None:
        old_line = lines[target_line]
        indent = old_line[:len(old_line) - len(old_line.lstrip())]
        if isinstance(value, bool):
            formatted = 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            formatted = str(value)
        else:
            formatted = str(value)
        lines[target_line] = f'{indent}{leaf}: {formatted}'
        path.write_text('\n'.join(lines))
    else:
        raw = yaml.safe_load(content) or {}
        target = raw
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[leaf] = value
        with open(path, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)


def _line_matches_path(lines: list[str], line_idx: int, keys: list[str]) -> bool:
    """Check if a YAML line at line_idx is under the parent hierarchy given by keys."""
    if len(keys) <= 1:
        return True

    target_indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
    parents_needed = list(keys[:-1])
    parents_needed.reverse()

    for i in range(line_idx - 1, -1, -1):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(stripped)
        if indent < target_indent:
            target_indent = indent
            if parents_needed and stripped.startswith(f'{parents_needed[0]}:'):
                parents_needed.pop(0)
                if not parents_needed:
                    return True
    return not parents_needed


class ConfigUpdateRequest(BaseModel):
    key_path: str
    value: Any


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest, request: Request):
    """Update a single value in config.yaml by dotted key path."""
    await _check_auth(request)
    _update_yaml_value(req.key_path, req.value)
    _reload_config()
    return {"updated": req.key_path, "value": req.value}


@app.get("/api/providers")
def list_providers():
    """List registered local providers and their availability."""
    config = _get_config()
    active = config.active_provider
    results = []
    for name in registry.list_providers():
        try:
            cls = registry.get_provider(name)
            settings = config.get_provider_settings(name)
            instance = cls(settings.model_dump())
            available = instance.is_available()
        except Exception:
            available = False
        results.append({
            "name": name,
            "available": available,
            "active": name == active,
            "local_only": getattr(cls, "local_only", False),
        })
    return {"providers": results}


@app.get("/api/tools")
def list_tools():
    """List registered tools."""
    tools = []
    for name in registry.list_tools():
        cls = registry.get_tool_class(name)
        tools.append({"name": name, "description": cls.description})
    return {"tools": tools}


@app.get("/api/metrics")
def get_metrics():
    """Return current metrics snapshot for all agents.

    Prefers the in-memory snapshot (agents started by this server process).
    Falls back to reading the latest row per agent from the SQLite store so
    that CLI-started agents (separate OS process) are always visible.
    """
    snap = metrics.snapshot()

    # If the server started the agents, in-memory data is authoritative.
    if snap["total_agents_tracked"] > 0:
        return snap

    # No in-memory data — try the persistent store (written by the CLI process).
    if _metrics_store is None:
        return snap

    try:
        import time as _time
        now = _time.time()

        # Fetch the most recent row per agent recorded in the last 24 hours.
        rows = _metrics_store.query(range_hours=24)

        # Build a synthetic snapshot keyed by agent name (latest row wins).
        latest: dict[str, dict] = {}
        for row in rows:
            latest[row["agent"]] = row

        agents_out: dict[str, dict] = {}
        total_ticks = 0
        total_errors = 0

        # Helper: check the job queue for pending/processing jobs for an agent.
        # Used to distinguish "idle-polling" from "actively processing a job".
        def _has_pending_job(agent_name: str) -> bool:
            if _job_queue is None:
                return False
            try:
                stats = _job_queue.get_stats(agent=agent_name)
                return (stats.get("pending", 0) + stats.get("processing", 0)) > 0
            except Exception:
                return False

        # Helper: get real uptime from the status file.
        def _real_uptime(agent_name: str) -> float | None:
            from secure_agents.core.agent_status import get_started_at
            started_at = get_started_at(agent_name)
            return round(now - started_at, 1) if started_at else None

        # 1. Agents present in the persistent store (have had ≥10 ticks).
        for agent_name, row in latest.items():
            is_running = is_running_externally(agent_name)
            ticks = row.get("ticks") or 0
            errors = row.get("errors") or 0
            latency_ms = row.get("latency_ms")
            total_ticks += ticks
            total_errors += errors

            # Dedup agents idle-poll the queue every 5 s while waiting for a job.
            # Distinguish that from actually processing a job so the UI is clear.
            if is_running and latency_ms and latency_ms >= 4500:
                # Tick time ≈ poll interval — agent is idle-waiting, not processing.
                agent_status = "waiting" if not _has_pending_job(agent_name) else "processing"
                display_latency = None  # hide poll-interval noise from latency column
            else:
                agent_status = "running" if is_running else "idle"
                display_latency = {"mean_ms": latency_ms} if latency_ms else None

            agents_out[agent_name] = {
                "ticks": ticks,
                "errors": errors,
                "error_rate_pct": round(errors / ticks * 100, 2) if ticks else 0,
                "running": is_running,
                "status": agent_status,
                "latency": display_latency,
                "last_recorded_at": row.get("ts"),
                "starts": 0,
                "stops": 0,
                "uptime_s": _real_uptime(agent_name) if is_running else round(now - row["ts"], 1),
                "run_count_today": 0,
                "run_count_total": 0,
            }

        # 2. Agents that are running externally but haven't flushed to DB yet
        #    (e.g. single-run agents that do all their work in one long tick).
        from secure_agents.core.agent_status import list_external
        for agent_name in list_external():
            if agent_name not in agents_out:
                agents_out[agent_name] = {
                    "ticks": 0,
                    "errors": 0,
                    "error_rate_pct": 0,
                    "running": True,
                    "status": "running",
                    "latency": None,
                    "last_recorded_at": None,
                    "starts": 0,
                    "stops": 0,
                    "uptime_s": _real_uptime(agent_name),
                    "run_count_today": 0,
                    "run_count_total": 0,
                }

        if not agents_out:
            return snap

        return {
            "server_uptime_s": snap["server_uptime_s"],
            "total_agents_tracked": len(agents_out),
            "total_ticks": total_ticks,
            "total_errors": total_errors,
            "agents": agents_out,
            "source": "persistent_store",
        }
    except Exception as exc:
        logger.warning("server.metrics_fallback_failed", error=str(exc))
        return snap


# ── Pipeline endpoints ────────────────────────────────────────────────────────

def _pipeline_agent_status(agent_name: str) -> dict:
    """Return running status + health summary for one agent within a pipeline."""
    is_running = (
        (agent_name in _running_agents and _running_agents[agent_name]["thread"].is_alive())
        or is_running_externally(agent_name)
    )
    return {"name": agent_name, "running": is_running}


def _compute_pipeline_progress(pipeline_name: str, pcfg: dict, agent_statuses: list[dict]) -> int:
    """Compute 0–100 progress percentage for a pipeline.

    With stage-by-stage execution, stage-2 agents are only alive while they
    have actual work — they do not idle-poll during stage 1.  Progress is
    driven by the pipeline-level status file (written by the CLI for the
    duration of the run) and per-agent status files (written only while each
    agent's thread is live).

    States:
      0%   — pipeline not running and no recent completed jobs
      25%  — stage 1 running
      50%  — stage 1 done (pending jobs in queue); stage 2 not started yet
      50–99% — stage 2 running; each finished agent adds (50/N)%
      100% — all done (no agents running, no pending jobs)
    """
    # Filter out confirm-gate dicts — only agent-list stages drive progress
    all_stages = pcfg.get("stages") or []
    stages = [s for s in all_stages if isinstance(s, list)]

    if not stages or len(stages) < 2:
        # No stage topology — simple running fraction
        running_count = sum(1 for s in agent_statuses if s["running"])
        total = len(agent_statuses)
        if running_count == total and total > 0:
            return 50
        return 25 if running_count > 0 else 0

    stage1_names = set(stages[0])
    parallel_names = [a for stage in stages[1:] for a in stage]
    total_parallel = len(parallel_names)

    by_name = {s["name"]: s["running"] for s in agent_statuses}

    stage1_running = any(by_name.get(n, False) for n in stage1_names)
    stage2_running = any(by_name.get(n, False) for n in parallel_names)

    # --- Not started or already complete ---
    if not stage1_running and not stage2_running:
        # Is this pipeline actively executing (CLI process still alive)?
        pipeline_active = is_pipeline_running(pipeline_name)

        if not pipeline_active:
            # No CLI process — check whether it ever ran by looking at queue
            if _job_queue is not None:
                try:
                    # Check for jobs created after the pipeline last started
                    started_at = get_pipeline_started_at(pipeline_name)
                    stats_per_agent: dict[str, dict] = {}
                    for aname in parallel_names:
                        per = _job_queue.get_stats(agent=aname)
                        for status, count in per.items():
                            stats_per_agent.setdefault(status, 0)
                            stats_per_agent[status] += count
                    completed = stats_per_agent.get("completed", 0)
                    pending   = stats_per_agent.get("pending", 0) + stats_per_agent.get("processing", 0)
                    if pending == 0 and completed >= total_parallel:
                        return 100  # all dedup jobs done
                except Exception:
                    pass
            return 0  # not started

        # Pipeline process is alive but no agents running — between stages
        # (stage 1 just finished, stage 2 hasn't started yet)
        return 50

    # --- Stage 1 in progress ---
    if stage1_running:
        return 25

    # --- Stage 2 in progress ---
    # Count how many stage-2 agents have already stopped (finished their job).
    stage2_stopped = sum(1 for n in parallel_names if not by_name.get(n, False))
    pct = stage2_stopped / max(total_parallel, 1)
    return min(50 + int(pct * 50), 99)  # cap at 99 until all done


@app.get("/api/pipelines")
def list_pipelines():
    """List all configured pipelines with per-agent run status, stages, and progress."""
    config = _reload_config()
    pipelines_cfg = getattr(config, "pipelines", {}) or {}
    result = []
    for name, pcfg in pipelines_cfg.items():
        agent_names = pcfg.get("agents", [])
        agent_statuses = [_pipeline_agent_status(a) for a in agent_names]
        all_running = bool(agent_statuses) and all(s["running"] for s in agent_statuses)
        any_running = any(s["running"] for s in agent_statuses)

        # Build stages response: list of lists of agent status objects.
        # Confirm-gate dicts are stripped — the gate state is surfaced
        # separately via the gate_pending / gate_message fields.
        raw_stages = pcfg.get("stages") or []
        stages_response: list[list[dict]] | None = None
        if raw_stages:
            by_name = {s["name"]: s for s in agent_statuses}
            stages_response = [
                [by_name[a] for a in stage if a in by_name]
                for stage in raw_stages
                if isinstance(stage, list)
            ] or None

        progress_pct = _compute_pipeline_progress(name, pcfg, agent_statuses)

        # Job queue stats: split by status so the UI shows meaningful numbers.
        # pending + processing = active work; completed = historical.
        queue_stats: dict[str, int] = {}
        if _job_queue is not None:
            try:
                all_stats = _job_queue.get_stats()
                # Aggregate per-agent stats for all agents in this pipeline
                for aname in agent_names:
                    per = _job_queue.get_stats(agent=aname)
                    for status, count in per.items():
                        queue_stats[status] = queue_stats.get(status, 0) + count
            except Exception:
                pass

        # Check for a pending confirmation gate
        gate = get_gate(name)

        result.append({
            "name": name,
            "description": pcfg.get("description", ""),
            "agents": agent_statuses,
            "all_running": all_running,
            "any_running": any_running,
            "stages": stages_response,
            "progress_pct": progress_pct,
            "queue_stats": queue_stats,
            "gate_pending": gate is not None,
            "gate_message": gate["message"] if gate else None,
        })
    return {"pipelines": result}


@app.post("/api/pipelines/{pipeline_name}/start")
async def start_pipeline(pipeline_name: str, request: Request):
    """Start all agents belonging to a pipeline."""
    await _check_auth(request)
    config = _reload_config()
    pipelines_cfg = getattr(config, "pipelines", {}) or {}
    if pipeline_name not in pipelines_cfg:
        raise HTTPException(404, "Pipeline not found")

    agent_names = pipelines_cfg[pipeline_name].get("agents", [])
    req = StartRequest(agents=agent_names)
    # Reuse the existing start_agents logic
    return await start_agents(req, request)


@app.post("/api/pipelines/{pipeline_name}/stop")
async def stop_pipeline(pipeline_name: str, request: Request):
    """Stop all running agents belonging to a pipeline."""
    await _check_auth(request)
    config = _reload_config()
    pipelines_cfg = getattr(config, "pipelines", {}) or {}
    if pipeline_name not in pipelines_cfg:
        raise HTTPException(404, "Pipeline not found")

    agent_names = pipelines_cfg[pipeline_name].get("agents", [])
    stopped = []
    for agent_name in agent_names:
        if agent_name in _running_agents:
            entry = _running_agents.get(agent_name)
            if entry and entry["thread"].is_alive():
                entry["agent"].request_stop()
                entry["thread"].join(timeout=5.0)
                _running_agents.pop(agent_name, None)
                stopped.append(agent_name)
    return {"stopped": stopped, "pipeline": pipeline_name}


class GateDecisionRequest(BaseModel):
    approved: bool


@app.post("/api/pipelines/{pipeline_name}/gate")
async def resolve_pipeline_gate(
    pipeline_name: str, req: GateDecisionRequest, request: Request
):
    """Approve or reject a pipeline confirmation gate from the dashboard."""
    await _check_auth(request)
    pipelines_cfg = getattr(_get_config(), "pipelines", {}) or {}
    if pipeline_name not in pipelines_cfg:
        raise HTTPException(404, "Pipeline not found")
    write_gate_approval(pipeline_name, req.approved)
    return {"pipeline": pipeline_name, "approved": req.approved}


# ── Agent toggle / logs / outputs ────────────────────────────────────────────

@app.patch("/api/agents/{agent_name}/toggle")
async def toggle_agent(agent_name: str, request: Request):
    """Toggle an agent's enabled/disabled state in config."""
    await _check_auth(request)
    _validate_agent_name_param(agent_name)
    config = _reload_config()
    agent_cfg = config.agents.get(agent_name, {})
    new_state = not agent_cfg.get("enabled", True)
    _update_yaml_value(f"agents.{agent_name}.enabled", new_state)
    _reload_config()
    return {"agent": agent_name, "enabled": new_state}


@app.get("/api/logs/{agent_name}")
def get_agent_logs(agent_name: str, lines: int = 200):
    """Get recent log entries for a specific agent from audit.log."""
    _validate_agent_name_param(agent_name)
    project_root = Path(_config_path).resolve().parent
    log_path = project_root / "logs" / "audit.log"
    if not log_path.exists():
        return {"lines": [], "total": 0}

    matched = []
    try:
        with open(log_path, "r") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                if agent_name in raw_line:
                    try:
                        entry = json.loads(raw_line)
                        matched.append(entry)
                    except json.JSONDecodeError:
                        matched.append({"raw": raw_line})
    except Exception:
        pass

    return {"lines": matched[-lines:], "total": len(matched)}


def _scan_output_dir(output_dir: Path) -> list[dict]:
    """Return a list of file info dicts for all files under output_dir."""
    files = []
    if not output_dir.exists():
        return files
    for item in sorted(output_dir.rglob("*")):
        if item.is_file():
            stat = item.stat()
            files.append({
                "name": str(item.relative_to(output_dir)),
                "path": str(item),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    return files


@app.get("/api/pipelines/{pipeline_name}/outputs")
def get_pipeline_outputs(pipeline_name: str):
    """List output files for all agents in a pipeline, grouped by agent."""
    config = _reload_config()
    pipelines_cfg = getattr(config, "pipelines", {}) or {}
    if pipeline_name not in pipelines_cfg:
        raise HTTPException(404, "Pipeline not found")

    pcfg = pipelines_cfg[pipeline_name]
    agent_names = pcfg.get("agents", [])
    sections = []

    for agent_name in agent_names:
        merged = config.get_agent_config(agent_name)
        output_root = merged.get("output_root")
        if not output_root:
            continue
        output_path = Path(output_root)
        csv_files = []
        if output_path.exists():
            for item in sorted(output_path.rglob("*.csv")):
                stat = item.stat()
                csv_files.append({
                    "name": str(item.relative_to(output_path)),
                    "path": str(item),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
        if csv_files:
            sections.append({"title": agent_name, "files": csv_files})

    return {"pipeline": pipeline_name, "sections": sections}


@app.get("/api/outputs")
def list_outputs():
    """List all output files organized by agent.

    Scans both the project-level ./output/ directory and each agent's
    ``output_root`` directory from config (so pipeline outputs are visible
    even when they write to external paths).
    """
    project_root = Path(_config_path).resolve().parent
    result: dict[str, list] = {}

    # 1. Scan ./output/ (legacy location)
    output_dir = project_root / "output"
    if output_dir.exists():
        for item in sorted(output_dir.rglob("*")):
            if item.is_file():
                rel = item.relative_to(output_dir)
                parts = rel.parts
                agent = parts[0] if len(parts) > 1 else "_root"
                if agent not in result:
                    result[agent] = []
                stat = item.stat()
                result[agent].append({
                    "name": str(rel),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })

    # 2. Scan each agent's output_root from config
    try:
        config = _get_config()
        for agent_name in config.agents:
            merged = config.get_agent_config(agent_name)
            output_root = merged.get("output_root")
            if not output_root:
                continue
            opath = Path(output_root)
            if not opath.exists() or opath == output_dir:
                continue
            for item in sorted(opath.rglob("*.csv")):
                if item.is_file():
                    bucket = agent_name
                    if bucket not in result:
                        result[bucket] = []
                    stat = item.stat()
                    result[bucket].append({
                        "name": str(item.relative_to(opath)),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })
    except Exception:
        pass

    return {"agents": result}


@app.get("/api/outputs/{path:path}")
def get_output(path: str):
    """Download/view a specific output file."""
    project_root = Path(_config_path).resolve().parent
    output_dir = project_root / "output"
    target = (output_dir / path).resolve()
    # Security: prevent path traversal
    if not str(target).startswith(str(output_dir.resolve())):
        raise HTTPException(403, "Access denied")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    content = target.read_text(errors="replace")
    if target.suffix == ".json":
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
    return PlainTextResponse(content)


@app.delete("/api/outputs/{path:path}")
async def delete_output(path: str, request: Request):
    """Delete a specific output file."""
    await _check_auth(request)
    project_root = Path(_config_path).resolve().parent
    output_dir = project_root / "output"
    target = (output_dir / path).resolve()
    if not str(target).startswith(str(output_dir.resolve())):
        raise HTTPException(403, "Access denied")
    if not target.exists():
        raise HTTPException(404, "File not found")
    target.unlink()
    return {"deleted": path}


# ── Audit Log ────────────────────────────────────────────────────────────────

@app.get("/api/audit-log")
def get_audit_log(limit: int = 100, offset: int = 0, agent: str | None = None):
    """Paginated audit log entries."""
    if agent:
        _validate_agent_name_param(agent)
    project_root = Path(_config_path).resolve().parent
    log_path = project_root / "logs" / "audit.log"
    if not log_path.exists():
        return {"entries": [], "total": 0, "limit": limit, "offset": offset}

    entries = []
    try:
        with open(log_path, "r") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    entry = {"event": "raw", "detail": raw_line}
                if agent and entry.get("agent") != agent and agent not in raw_line:
                    continue
                entries.append(entry)
    except Exception:
        pass

    entries.reverse()
    total = len(entries)
    page = entries[offset:offset + limit]
    return {"entries": page, "total": total, "limit": limit, "offset": offset}


# ── Metrics History + Export ─────────────────────────────────────────────────

@app.get("/api/metrics/history")
def get_metrics_history(agent: str | None = None, range: int = 24):
    if _metrics_store is None:
        return {"data": [], "range_hours": range}
    data = _metrics_store.query(agent=agent, range_hours=range)
    return {"data": data, "range_hours": range}


@app.get("/api/metrics/hourly")
def get_metrics_hourly(agent: str | None = None, range: int = 168):
    if _metrics_store is None:
        return {"data": [], "range_hours": range}
    data = _metrics_store.query_hourly(agent=agent, range_hours=range)
    return {"data": data, "range_hours": range}


@app.get("/api/metrics/export")
def export_metrics(agent: str | None = None, range: int = 24, format: str = "csv"):
    if _metrics_store is None:
        return PlainTextResponse("No metrics data available", status_code=404)
    csv_data = _metrics_store.export_csv(agent=agent, range_hours=range)
    return PlainTextResponse(
        csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=metrics_export.csv"},
    )


# ── Dead-Letter Queue ────────────────────────────────────────────────────────

@app.get("/api/queue/dlq")
def list_dlq(agent: str | None = None, limit: int = 100, offset: int = 0):
    if _job_queue is None:
        return {"entries": [], "total": 0}
    entries = _job_queue.list_dlq(agent=agent, limit=limit, offset=offset)
    total = _job_queue.dlq_count(agent=agent)
    return {"entries": entries, "total": total}


@app.post("/api/queue/dlq/{job_id}/retry")
async def retry_dlq_job(job_id: str, request: Request):
    await _check_auth(request)
    if _job_queue is None:
        raise HTTPException(500, "Job queue not initialized")
    job = _job_queue.retry_from_dlq(job_id)
    if job is None:
        raise HTTPException(404, "DLQ job not found")
    return {"retried": True, "new_job_id": job.id}


@app.get("/api/queue/stats")
def get_queue_stats():
    if _job_queue is None:
        return {"stats": {}, "dlq_count": 0}
    stats = _job_queue.get_stats()
    dlq_count = _job_queue.dlq_count()
    return {"stats": stats, "dlq_count": dlq_count}


# ── Triggers ────────────────────────────────────────────────────────────────

@app.get("/api/triggers")
def list_triggers():
    if _trigger_manager is None:
        return {"triggers": []}
    return {"triggers": _trigger_manager.list_triggers()}


class TriggerFireRequest(BaseModel):
    agent: str
    trigger_name: str = "manual"


@app.post("/api/triggers/fire")
async def fire_manual_trigger(req: TriggerFireRequest, request: Request):
    await _check_auth(request)
    _validate_agent_name_param(req.agent)
    if _trigger_manager is None:
        raise HTTPException(500, "Trigger manager not initialized")
    triggers = _trigger_manager.list_triggers()
    matched = [t for t in triggers if t["name"] == req.agent]
    if not matched:
        raise HTTPException(404, "No trigger registered for this agent")
    if _job_queue:
        _job_queue.enqueue(req.agent, {"trigger": "manual"})
    return {"fired": True, "agent": req.agent}


# ── Bootstrap helpers ────────────────────────────────────────────────────────

def _ensure_config(config_path: str) -> str:
    target = Path(config_path)
    if target.exists():
        return config_path

    candidates = [
        Path.cwd() / "config.example.yaml",
        Path(__file__).resolve().parents[3] / "config.example.yaml",
    ]
    for src in candidates:
        if src.exists():
            shutil.copy2(src, target)
            logger.info("bootstrap.config_copied", src=str(src), dest=str(target))
            return config_path

    return config_path


def _ensure_ollama() -> None:
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return

    import httpx
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        if resp.status_code == 200:
            return
    except Exception:
        pass

    brew_bin = shutil.which("brew")
    if brew_bin:
        try:
            subprocess.run(
                [brew_bin, "services", "start", "ollama"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            logger.info("bootstrap.ollama_started", method="brew_services")
            return
        except Exception:
            pass

    try:
        subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("bootstrap.ollama_started", method="popen")
    except Exception as e:
        logger.warning("bootstrap.ollama_start_failed", error=str(e))


# ── Server launcher ─────────────────────────────────────────────────────────

def run_server(config_path: str = "config.yaml", host: str = "127.0.0.1", port: int = 8420,
               open_browser: bool = True):
    """Launch the dashboard server.

    Security:
    - Always binds to 127.0.0.1 (localhost only)
    - Generates a per-session auth token printed to terminal
    - Adds strict CORS middleware (localhost origins only)
    """
    global _config_path, _metrics_store, _job_queue, _auth_token

    # Security: force localhost binding
    if host != "127.0.0.1" and host != "localhost":
        logger.warning("server.remote_bind_blocked",
                      requested_host=host,
                      msg="Forcing bind to 127.0.0.1 — remote access is not allowed")
        host = "127.0.0.1"

    setup_logging()

    # Generate per-session auth token
    _auth_token = _generate_auth_token()

    # Add strict CORS middleware — localhost only
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["X-Auth-Token", "Content-Type"],
    )

    # Bootstrap
    config_path = _ensure_config(config_path)
    _config_path = config_path
    # Configure credential backend from config (Keychain on Mac, encrypted
    # file in VMs).  The dashboard runs non-interactively, so we forbid
    # interactive prompts here — the master passphrase must come from the
    # SECURE_AGENTS_MASTER_KEY env var when running headless.
    try:
        bootstrap_cfg = load_config(config_path)
        from secure_agents.core.credentials import configure_credentials
        configure_credentials(
            backend=bootstrap_cfg.credentials.backend,
            store_path=bootstrap_cfg.credentials.store_path,
            interactive=False,
        )
        # Auto-start Ollama on bootstrap only when it's the active provider —
        # other local providers (llama.cpp, vLLM, LM Studio, LocalAI) are
        # managed by the user.
        if bootstrap_cfg.active_provider == "ollama":
            _ensure_ollama()
    except Exception:
        pass
    discover_all()

    # Initialize persistent metrics store
    project_root = Path(config_path).resolve().parent
    try:
        from secure_agents.core.metrics_store import get_store
        _metrics_store = get_store(str(project_root / "data" / "metrics.db"))
        metrics.set_store(_metrics_store)
    except Exception as e:
        logger.warning("server.metrics_store_init_failed", error=str(e))

    # Initialize job queue
    try:
        config = _get_config()
        from secure_agents.core.job_queue import JobQueue
        _job_queue = JobQueue(
            db_path=str(project_root / config.queue.db_path),
            max_retries=config.queue.max_retries,
            retry_delay=config.queue.retry_delay_seconds,
        )
    except Exception as e:
        logger.warning("server.job_queue_init_failed", error=str(e))

    # Initialize trigger manager
    global _trigger_manager
    try:
        from secure_agents.core.trigger_manager import TriggerManager
        _trigger_manager = TriggerManager()
        config = _get_config()
        for agent_name, agent_cfg in config.agents.items():
            triggers_cfg = agent_cfg.get("triggers", [])
            for trig_cfg in triggers_cfg:
                trig_type = trig_cfg.get("type", "manual")
                def _make_callback(a_name, t_type):
                    def _trigger_callback(**kwargs):
                        if _job_queue:
                            _job_queue.enqueue(a_name, {"trigger": t_type, **kwargs})
                        logger.info("trigger.fired", agent=a_name, trigger_type=t_type)
                    return _trigger_callback
                _trigger_manager.register(agent_name, trig_cfg, _make_callback(agent_name, trig_type))
        _trigger_manager.start_all()
        logger.info("server.triggers_initialized", count=len(_trigger_manager.list_triggers()))
    except Exception as e:
        logger.warning("server.trigger_init_failed", error=str(e))

    # Print auth token to terminal
    print(f"\n{'='*60}")
    print(f"  Secure Agents Dashboard")
    print(f"  http://{host}:{port}")
    print(f"{'='*60}")
    print(f"  Auth token (auto-injected into dashboard):")
    print(f"  {_auth_token}")
    print(f"{'='*60}\n")

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")
