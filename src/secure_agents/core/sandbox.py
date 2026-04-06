"""Sandbox execution layer for isolated document processing.

Provides Docker-based isolation. When sandbox is enabled (the default),
Docker MUST be available — there is no fallback to native execution.
Each execution creates an ephemeral environment that is destroyed after use.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()


def _docker_available() -> bool:
    """Check if Docker is installed and running."""
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


def run_in_sandbox(
    script: str,
    input_data: dict,
    timeout: int = 300,
    sandbox_enabled: bool = True,
) -> dict:
    """Execute a Python script in an isolated Docker container.

    When sandbox_enabled is True (the default), Docker MUST be available.
    There is NO fallback to native execution — if Docker is missing, this
    raises a hard error. This is intentional: silent degradation to native
    execution would defeat the purpose of sandboxing.

    The container runs with:
    - No network access (--network=none)
    - Read-only input mount
    - Write-only output mount
    - Memory limit (512MB)
    - CPU throttling (50% of one core)
    - Read-only root filesystem
    - Automatic destruction after completion

    Args:
        script: Python code to execute. Must write JSON to /output/result.json.
        input_data: Data passed to the script as /input/data.json.
        timeout: Maximum execution time in seconds.
        sandbox_enabled: Whether Docker isolation is required.

    Returns:
        Parsed JSON result from the script.

    Raises:
        RuntimeError: If sandbox is enabled but Docker is not available.
    """
    if sandbox_enabled:
        if not _docker_available():
            raise RuntimeError(
                "Sandbox is enabled but Docker is not available. "
                "Either install and start Docker, or explicitly set "
                "security.sandbox_enabled: false in config.yaml "
                "(NOT RECOMMENDED — disabling the sandbox allows "
                "untrusted documents to be parsed on the host)."
            )
        return _run_docker(script, input_data, timeout)

    # Sandbox explicitly disabled — warn and refuse.
    # There is no subprocess fallback. Code that needs to run without
    # Docker must call the parsing libraries directly (as document_parser
    # does when sandbox is disabled).
    raise RuntimeError(
        "Sandbox is disabled. Use direct library calls instead of "
        "run_in_sandbox(). This function only supports Docker execution."
    )


def _run_docker(script: str, input_data: dict, timeout: int) -> dict:
    """Run script inside a Docker container with no network access."""
    import docker

    client = docker.from_env()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_dir = tmp / "input"
        output_dir = tmp / "output"
        input_dir.mkdir()
        output_dir.mkdir()

        # Write input data and script
        (input_dir / "data.json").write_text(json.dumps(input_data))
        (input_dir / "run.py").write_text(script)

        try:
            result = client.containers.run(
                image="secure-agents-sandbox",
                command=["python", "/input/run.py"],
                volumes={
                    str(input_dir): {"bind": "/input", "mode": "ro"},
                    str(output_dir): {"bind": "/output", "mode": "rw"},
                },
                network_disabled=True,
                mem_limit="512m",
                cpu_period=100000,
                cpu_quota=50000,  # 50% of one CPU
                read_only=True,
                tmpfs={"/tmp": "size=100m"},
                remove=True,
                timeout=timeout,
            )
            logger.info("sandbox.docker.completed")

            result_file = output_dir / "result.json"
            if result_file.exists():
                return json.loads(result_file.read_text())
            # If no result file, try to parse container stdout
            return json.loads(result.decode("utf-8"))

        except Exception as e:
            logger.error("sandbox.docker.error", error=str(e))
            raise RuntimeError(f"Sandbox execution failed: {e}") from e
