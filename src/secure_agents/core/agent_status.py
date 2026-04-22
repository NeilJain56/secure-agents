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
