"""FastAPI backend for the Secure Agents dashboard.

Provides a REST API for managing agents: listing, health checks,
setup validation, credential storage, start/stop control, metrics,
outputs, audit log, triggers, and dead-letter queue.
"""

from __future__ import annotations

import json
import os
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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from secure_agents.core.builder import build_agent, discover_all
from secure_agents.core.config import load_config, AppConfig
from secure_agents.core.credentials import get_credential, store_credential, get_oauth2_token
from secure_agents.core.logger import setup_logging
from secure_agents.core.metrics import metrics
from secure_agents.core.registry import registry

logger = structlog.get_logger()

app = FastAPI(title="Secure Agents", docs_url=None, redoc_url=None)

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




# ── Health check logic ───────────────────────────────────────────────────────

def _check_agent_health(agent_name: str, config: AppConfig) -> dict:
    """Run health checks for an agent: provider, credentials, tools."""
    merged = config.get_agent_config(agent_name)
    checks = []

    # 1. Check provider
    provider_name = merged.get("provider", {}).get("override", config.provider.active)
    try:
        provider_cls = registry.get_provider(provider_name)
        provider_settings = getattr(config.provider, provider_name)
        provider = provider_cls(provider_settings.model_dump())
        if provider.is_available():
            checks.append({"name": f"Provider ({provider_name})", "status": "ok", "detail": "Connected and ready"})
        elif provider_name == "ollama":
            # Check if Ollama is even installed
            if shutil.which("ollama"):
                _ensure_ollama()  # try to start it automatically
                # Re-check after attempting start
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
            else:
                checks.append({"name": f"Provider ({provider_name})", "status": "error",
                               "detail": "Ollama is not installed",
                               "setup_action": "ollama_install"})
        else:
            checks.append({"name": f"Provider ({provider_name})", "status": "error",
                           "detail": f"{provider_name.title()} API is not reachable",
                           "setup_action": "provider"})
    except Exception as e:
        checks.append({"name": f"Provider ({provider_name})", "status": "error", "detail": str(e)})

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

    # 3. Check if provider needs API key
    if provider_name in ("anthropic", "openai", "gemini"):
        key_name = f"{provider_name}_api_key"
        if get_credential(key_name):
            checks.append({"name": f"{provider_name.title()} API key", "status": "ok",
                           "detail": "Found in keychain/env"})
        else:
            checks.append({"name": f"{provider_name.title()} API key", "status": "error",
                           "detail": f"No API key for {provider_name}",
                           "setup_action": "credential",
                           "setup_key": key_name})

    all_ok = all(c["status"] == "ok" for c in checks)
    return {"healthy": all_ok, "checks": checks}


# ── API Routes ─────���─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    """Serve the single-page dashboard."""
    html_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html_path.read_text())


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
        is_running = name in _running_agents and _running_agents[name]["thread"].is_alive()

        email_username = merged.get("email", {}).get("imap", {}).get("username", "")
        auth_method = merged.get("email", {}).get("imap", {}).get("auth_method", "app_password")

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
            "provider": merged.get("provider", {}).get("override", config.provider.active),
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
    config = _reload_config()
    if agent_name not in registry.list_agents():
        raise HTTPException(404, f"Agent '{agent_name}' not found")
    return _check_agent_health(agent_name, config)


class StartRequest(BaseModel):
    agents: list[str]


@app.post("/api/agents/start")
def start_agents(req: StartRequest):
    """Start one or more agents in background threads with concurrency limits."""
    config = _reload_config()
    started = []
    errors = []

    # Count currently running agents
    running_count = sum(1 for e in _running_agents.values() if e["thread"].is_alive())

    for name in req.agents:
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
            errors.append({"agent": name, "error": str(e)})

    return {"started": started, "errors": errors}


@app.post("/api/agents/{agent_name}/stop")
def stop_agent(agent_name: str):
    """Stop a running agent."""
    if agent_name not in _running_agents:
        raise HTTPException(404, f"Agent '{agent_name}' is not running")

    entry = _running_agents.get(agent_name)
    if entry is None:
        raise HTTPException(404, f"Agent '{agent_name}' is not running")
    entry["agent"].request_stop()
    entry["thread"].join(timeout=5.0)
    _running_agents.pop(agent_name, None)
    return {"stopped": agent_name}


@app.post("/api/agents/stop-all")
def stop_all_agents():
    """Stop all running agents."""
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
def save_credential(req: CredentialRequest):
    """Store a credential in the macOS Keychain."""
    if store_credential(req.key, req.value):
        return {"stored": True, "key": req.key}
    raise HTTPException(500, "Failed to store credential")


class TestEmailRequest(BaseModel):
    username: str = ""
    auth_method: str = "app_password"
    host: str = "imap.gmail.com"
    port: int = 993


@app.post("/api/test-email")
def test_email_connection(req: TestEmailRequest):
    """Test IMAP connection with current credentials.

    Actually connects to the mail server and authenticates to verify everything works.
    """
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
        raise HTTPException(500, "imapclient not installed")

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

            # If we get here, authentication succeeded - grab folder list as proof
            folders = client.list_folders()
            folder_count = len(folders) if folders else 0
            return {"success": True, "detail": f"Connected to {host} as {username} ({folder_count} folders)"}

    except Exception as e:
        error_msg = str(e)
        # Provide friendlier error messages for common failures
        if "AUTHENTICATIONFAILED" in error_msg.upper() or "Invalid credentials" in error_msg:
            if auth_method == "oauth2":
                error_msg = "OAuth2 authentication failed. Token may be expired. Re-run: secure-agents auth gmail client_secrets.json"
            else:
                error_msg = "Authentication failed. Check your email and app password. For Gmail, you need a 16-character App Password (not your regular password)."
        elif "Connection refused" in error_msg or "timed out" in error_msg.lower():
            error_msg = f"Could not connect to {host}:{port}. Check your network connection."
        return {"success": False, "error": error_msg}


def _update_yaml_value(key_path: str, value: Any) -> None:
    """Update a value in config.yaml while preserving comments and formatting.

    Uses line-level regex replacement for known leaf keys. Falls back to
    full YAML rewrite only if the key can't be found in the file.
    """
    import re

    path = Path(_config_path)
    if not path.exists():
        path.write_text("")

    content = path.read_text()
    keys = key_path.split(".")
    leaf = keys[-1]

    # Try line-level replacement: find "  leaf: old_value" and replace
    # Build an indent-aware pattern: the leaf key at any indent level
    pattern = re.compile(
        rf'^(\s*{re.escape(leaf)}\s*:\s*)(.+)$',
        re.MULTILINE,
    )

    # To handle multiple keys with the same leaf name (e.g. "username" under
    # both imap and smtp), we need context. Walk through the file and find
    # the match that appears under the right parent hierarchy.
    lines = content.split('\n')
    target_line = None

    # Build expected parent path to disambiguate
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f'{leaf}:'):
            # Check if the parent keys match by walking backwards
            if _line_matches_path(lines, i, keys):
                target_line = i
                break

    if target_line is not None:
        # Replace just this line, preserving indent and any inline comment
        old_line = lines[target_line]
        indent = old_line[:len(old_line) - len(old_line.lstrip())]
        # Format value
        if isinstance(value, bool):
            formatted = 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            formatted = str(value)
        else:
            formatted = str(value)
        lines[target_line] = f'{indent}{leaf}: {formatted}'
        path.write_text('\n'.join(lines))
    else:
        # Fallback: full YAML rewrite (loses comments but always works)
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
def update_config(req: ConfigUpdateRequest):
    """Update a single value in config.yaml by dotted key path."""
    _update_yaml_value(req.key_path, req.value)
    _reload_config()
    return {"updated": req.key_path, "value": req.value}


class ProviderSwitchRequest(BaseModel):
    provider: str


@app.post("/api/provider")
def switch_provider(req: ProviderSwitchRequest):
    """Switch the active LLM provider."""
    valid = registry.list_providers()
    if req.provider not in valid:
        raise HTTPException(400, f"Unknown provider '{req.provider}'. Available: {valid}")
    _update_yaml_value("provider.active", req.provider)
    _reload_config()
    return {"active": req.provider}


@app.get("/api/providers")
def list_providers():
    """List providers and their availability."""
    config = _get_config()
    providers = []
    for name in registry.list_providers():
        try:
            cls = registry.get_provider(name)
            settings = getattr(config.provider, name)
            instance = cls(settings.model_dump())
            available = instance.is_available()
        except Exception:
            available = False
        providers.append({"name": name, "available": available,
                          "active": name == config.provider.active})
    return {"providers": providers}


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
    """Return current metrics snapshot for all agents."""
    return metrics.snapshot()


# ── Agent toggle / logs / outputs ────────────────────────────────────────────

@app.patch("/api/agents/{agent_name}/toggle")
def toggle_agent(agent_name: str):
    """Toggle an agent's enabled/disabled state in config."""
    config = _reload_config()
    agent_cfg = config.agents.get(agent_name, {})
    new_state = not agent_cfg.get("enabled", True)
    _update_yaml_value(f"agents.{agent_name}.enabled", new_state)
    _reload_config()
    return {"agent": agent_name, "enabled": new_state}


@app.get("/api/logs/{agent_name}")
def get_agent_logs(agent_name: str, lines: int = 200):
    """Get recent log entries for a specific agent from audit.log."""
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

    # Return last N entries
    return {"lines": matched[-lines:], "total": len(matched)}


@app.get("/api/outputs")
def list_outputs():
    """List all output files organized by agent."""
    project_root = Path(_config_path).resolve().parent
    output_dir = project_root / "output"
    if not output_dir.exists():
        return {"agents": {}}

    result: dict[str, list] = {}
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
def delete_output(path: str):
    """Delete a specific output file."""
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

    # Return in reverse chronological order
    entries.reverse()
    total = len(entries)
    page = entries[offset:offset + limit]
    return {"entries": page, "total": total, "limit": limit, "offset": offset}


# ── Metrics History + Export ─────────────────────────────────────────────────

@app.get("/api/metrics/history")
def get_metrics_history(agent: str | None = None, range: int = 24):
    """Get time-series metrics data points."""
    if _metrics_store is None:
        return {"data": [], "range_hours": range}
    data = _metrics_store.query(agent=agent, range_hours=range)
    return {"data": data, "range_hours": range}


@app.get("/api/metrics/hourly")
def get_metrics_hourly(agent: str | None = None, range: int = 168):
    """Get hourly rollup of metrics."""
    if _metrics_store is None:
        return {"data": [], "range_hours": range}
    data = _metrics_store.query_hourly(agent=agent, range_hours=range)
    return {"data": data, "range_hours": range}


@app.get("/api/metrics/export")
def export_metrics(agent: str | None = None, range: int = 24, format: str = "csv"):
    """Export metrics as CSV."""
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
    """View dead-letter queue entries."""
    if _job_queue is None:
        return {"entries": [], "total": 0}
    entries = _job_queue.list_dlq(agent=agent, limit=limit, offset=offset)
    total = _job_queue.dlq_count(agent=agent)
    return {"entries": entries, "total": total}


@app.post("/api/queue/dlq/{job_id}/retry")
def retry_dlq_job(job_id: str):
    """Retry a dead-letter queue job."""
    if _job_queue is None:
        raise HTTPException(500, "Job queue not initialized")
    job = _job_queue.retry_from_dlq(job_id)
    if job is None:
        raise HTTPException(404, f"DLQ job '{job_id}' not found")
    return {"retried": True, "new_job_id": job.id}


@app.get("/api/queue/stats")
def get_queue_stats():
    """Get job queue statistics."""
    if _job_queue is None:
        return {"stats": {}, "dlq_count": 0}
    stats = _job_queue.get_stats()
    dlq_count = _job_queue.dlq_count()
    return {"stats": stats, "dlq_count": dlq_count}


# ── Triggers ────────────────────────────────────────────────────────────────

@app.get("/api/triggers")
def list_triggers():
    """List all registered triggers and their status."""
    if _trigger_manager is None:
        return {"triggers": []}
    return {"triggers": _trigger_manager.list_triggers()}


class TriggerFireRequest(BaseModel):
    agent: str
    trigger_name: str = "manual"


@app.post("/api/triggers/fire")
def fire_manual_trigger(req: TriggerFireRequest):
    """Manually fire a trigger for an agent."""
    if _trigger_manager is None:
        raise HTTPException(500, "Trigger manager not initialized")
    triggers = _trigger_manager.list_triggers()
    matched = [t for t in triggers if t["name"] == req.agent]
    if not matched:
        raise HTTPException(404, f"No trigger registered for agent '{req.agent}'")
    # Enqueue a job directly as manual fire
    if _job_queue:
        _job_queue.enqueue(req.agent, {"trigger": "manual"})
    return {"fired": True, "agent": req.agent}


# ── Bootstrap helpers ────────────────────────────────────────────────────────

def _ensure_config(config_path: str) -> str:
    """Copy config.example.yaml → config.yaml if the target doesn't exist."""
    target = Path(config_path)
    if target.exists():
        return config_path

    # Walk upward from CWD and the package dir looking for the example file
    candidates = [
        Path.cwd() / "config.example.yaml",
        Path(__file__).resolve().parents[3] / "config.example.yaml",  # repo root
    ]
    for src in candidates:
        if src.exists():
            shutil.copy2(src, target)
            logger.info("bootstrap.config_copied", src=str(src), dest=str(target))
            return config_path

    return config_path  # no example found, load_config will use defaults


def _ensure_ollama() -> None:
    """If Ollama is installed but not serving, start it as a background service."""
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return  # not installed, nothing we can do

    import httpx
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        if resp.status_code == 200:
            return  # already running
    except Exception:
        pass

    # Prefer brew services (runs as a proper background daemon, doesn't block terminal)
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

    # Fallback: start ollama serve detached
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
    """Launch the dashboard server."""
    global _config_path, _metrics_store, _job_queue

    setup_logging()

    # Bootstrap: ensure config file and local provider are ready
    config_path = _ensure_config(config_path)
    _config_path = config_path
    _ensure_ollama()

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
        # Register triggers from config
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

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")
