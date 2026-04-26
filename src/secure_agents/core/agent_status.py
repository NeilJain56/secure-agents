"""Cross-process agent status tracking via lightweight JSON status files.

Problem: the dashboard (server.py) and the CLI (cli.py) are separate OS
processes.  The dashboard's ``_running_agents`` dict only knows about agents
it started itself.  When a user runs ``secure-agents start ...`` from the
terminal, the dashboard has no way to see those threads.

Solution: whichever process starts an agent writes a tiny JSON file to
``data/running/{agent_name}.json`` containing its PID and a start
timestamp.  On stop it removes the file.  Any process (dashboard or CLI)
can read these files to discover externally-started agents.  We verify the
stored PID is still alive before reporting the agent as running, so stale
files from crashed processes are handled correctly.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Resolved at call time so callers don't need to pass a path
_DEFAULT_STATUS_DIR = Path("./data/running")


def _dir() -> Path:
    d = _DEFAULT_STATUS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_status(agent_name: str) -> None:
    """Record that *this* process is running ``agent_name``."""
    path = _dir() / f"{agent_name}.json"
    path.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}))


def clear_status(agent_name: str) -> None:
    """Remove the status file for ``agent_name`` (called on stop)."""
    try:
        (_dir() / f"{agent_name}.json").unlink(missing_ok=True)
    except Exception:
        pass


def is_running_externally(agent_name: str) -> bool:
    """Return True if another process has ``agent_name`` marked as running.

    We check that the stored PID is still alive.  Stale files from crashed
    processes are silently removed.
    """
    path = _dir() / f"{agent_name}.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        pid = data.get("pid")
        if not pid:
            return False
        if pid == os.getpid():
            # Same process — the caller already knows (via _running_agents)
            return False
        # Send signal 0 — raises if process doesn't exist
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # Process is dead — clean up the stale file
        path.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def list_external() -> list[str]:
    """Return names of all agents that appear to be running externally."""
    result = []
    try:
        for f in _dir().glob("*.json"):
            name = f.stem
            if is_running_externally(name):
                result.append(name)
    except Exception:
        pass
    return result


def write_pipeline_status(pipeline_name: str) -> None:
    """Record that *this* process is running the named pipeline."""
    write_status(f"_pipeline_{pipeline_name}")


def clear_pipeline_status(pipeline_name: str) -> None:
    """Remove the pipeline-level status file on completion."""
    clear_status(f"_pipeline_{pipeline_name}")


def is_pipeline_running(pipeline_name: str) -> bool:
    """Return True if this pipeline appears to be running in another process."""
    return is_running_externally(f"_pipeline_{pipeline_name}")


def get_pipeline_started_at(pipeline_name: str) -> float | None:
    """Return the unix timestamp when this pipeline was last started, or None."""
    return get_started_at(f"_pipeline_{pipeline_name}")


def write_gate(pipeline_name: str, message: str) -> None:
    """Write a confirmation gate file — pipeline is paused waiting for approval."""
    path = _dir() / f"_gate_{pipeline_name}.json"
    path.write_text(json.dumps({
        "pending": True,
        "message": message,
        "started_at": time.time(),
    }))


def clear_gate(pipeline_name: str) -> None:
    """Remove the gate file after the gate is resolved."""
    try:
        (_dir() / f"_gate_{pipeline_name}.json").unlink(missing_ok=True)
    except Exception:
        pass


def get_gate(pipeline_name: str) -> dict | None:
    """Return gate state dict if a confirmation gate is pending, else None."""
    path = _dir() / f"_gate_{pipeline_name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if data.get("pending") else None
    except Exception:
        return None


def write_gate_approval(pipeline_name: str, approved: bool) -> None:
    """Write an approval/rejection file for the dashboard → CLI signal."""
    path = _dir() / f"_gate_approve_{pipeline_name}.json"
    path.write_text(json.dumps({"approved": approved, "at": time.time()}))


def consume_gate_approval(pipeline_name: str) -> bool | None:
    """Read and delete the approval file.  Returns True/False/None (no file)."""
    path = _dir() / f"_gate_approve_{pipeline_name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        path.unlink(missing_ok=True)
        return bool(data.get("approved"))
    except Exception:
        path.unlink(missing_ok=True)
        return None


def get_started_at(agent_name: str) -> float | None:
    """Return the unix timestamp when *agent_name* was started, or None.

    Reads from the status file written by ``write_status()``.  Returns None
    if the agent is not running or the file cannot be read.
    """
    path = _dir() / f"{agent_name}.json"
    try:
        data = json.loads(path.read_text())
        return data.get("started_at")
    except Exception:
        return None
