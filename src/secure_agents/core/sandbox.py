"""Sandbox execution layer for isolated document processing.

Provides Docker-based isolation when available, with a subprocess fallback.
Each execution creates an ephemeral environment that is destroyed after use.
"""

from __future__ import annotations

import json
import subprocess
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
    sandbox_enabled: bool = False,
) -> dict:
    """Execute a Python script in an isolated environment.

    If Docker is available and sandbox_enabled, runs in a container with:
    - No network access
    - Read-only input mount
    - Write-only output mount
    - Automatic destruction after completion

    Otherwise, falls back to a subprocess with a timeout.

    Args:
        script: Python code to execute. Must write JSON to /output/result.json
                (Docker) or stdout (subprocess).
        input_data: Data passed to the script as /input/data.json.
        timeout: Maximum execution time in seconds.
        sandbox_enabled: Whether to attempt Docker isolation.

    Returns:
        Parsed JSON result from the script.
    """
    if sandbox_enabled and _docker_available():
        return _run_docker(script, input_data, timeout)
    return _run_subprocess(script, input_data, timeout)


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


def _run_subprocess(script: str, input_data: dict, timeout: int) -> dict:
    """Fallback: run script in a subprocess with timeout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "data.json").write_text(json.dumps(input_data))

        # Wrap the script to read input and print JSON output
        wrapper = f"""
import json, sys
sys.path.insert(0, '.')
INPUT_PATH = '{tmp / "data.json"}'
with open(INPUT_PATH) as f:
    input_data = json.load(f)

{script}
"""
        script_path = tmp / "run.py"
        script_path.write_text(wrapper)

        try:
            result = subprocess.run(
                ["python", str(script_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(tmp),
            )

            if result.returncode != 0:
                logger.error("sandbox.subprocess.error", stderr=result.stderr[:500])
                raise RuntimeError(f"Script failed: {result.stderr[:500]}")

            return json.loads(result.stdout)

        except subprocess.TimeoutExpired:
            logger.error("sandbox.subprocess.timeout", timeout=timeout)
            raise RuntimeError(f"Script timed out after {timeout}s")
        except json.JSONDecodeError:
            logger.error("sandbox.subprocess.bad_output")
            raise RuntimeError("Script did not produce valid JSON output")
