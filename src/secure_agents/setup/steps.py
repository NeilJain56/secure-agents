"""Individual setup step implementations. Each is idempotent."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger()


class StepResult:
    __slots__ = ("status", "message")

    def __init__(self, status: str, message: str):
        self.status = status  # "ok" (already done), "done" (just completed), "skipped", "error"
        self.message = message

    @staticmethod
    def ok(msg: str) -> StepResult:
        return StepResult("ok", msg)

    @staticmethod
    def done(msg: str) -> StepResult:
        return StepResult("done", msg)

    @staticmethod
    def skipped(msg: str) -> StepResult:
        return StepResult("skipped", msg)

    @staticmethod
    def error(msg: str) -> StepResult:
        return StepResult("error", msg)


# ── System packages ──────────────────────────────────────────────────────────

def ensure_homebrew() -> StepResult:
    """Ensure Homebrew is installed."""
    if shutil.which("brew"):
        return StepResult.ok("Homebrew installed")
    try:
        subprocess.run(
            ["/bin/bash", "-c",
             '$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)'],
            check=True,
        )
        # Apple Silicon path
        brew_path = Path("/opt/homebrew/bin/brew")
        if brew_path.exists():
            subprocess.run(
                ["/bin/bash", "-c", f'eval "$({brew_path} shellenv)"'],
                check=False,
            )
        return StepResult.done("Homebrew installed")
    except Exception as e:
        return StepResult.error(f"Failed to install Homebrew: {e}")


def ensure_homebrew_package(package: str) -> StepResult:
    """Install a Homebrew package if not present."""
    if shutil.which(package):
        return StepResult.ok(f"{package} already installed")
    brew = shutil.which("brew")
    if not brew:
        return StepResult.error(f"Homebrew not available -- cannot install {package}")
    try:
        subprocess.run([brew, "install", package], check=True)
        return StepResult.done(f"Installed {package}")
    except subprocess.CalledProcessError as e:
        return StepResult.error(f"brew install {package} failed: {e}")


# ── Pip extras ───────────────────────────────────────────────────────────────

def ensure_pip_extra(extra: str, project_root: Path) -> StepResult:
    """Install a pip optional-dependency group if not already present."""
    venv_pip = project_root / ".venv" / "bin" / "pip"
    pip_cmd = str(venv_pip) if venv_pip.exists() else "pip"

    # Map extra names to a package we can check for
    check_packages = {
        "anthropic": "anthropic",
        "openai": "openai",
        "gemini": "google-genai",
        "gmail-oauth": "google-auth-oauthlib",
    }
    check_pkg = check_packages.get(extra)
    if check_pkg:
        result = subprocess.run(
            [pip_cmd, "show", check_pkg],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return StepResult.ok(f"{extra} dependencies already installed")

    try:
        subprocess.run(
            [pip_cmd, "install", f"secure-agents[{extra}]", "--quiet"],
            check=True, cwd=str(project_root),
        )
        return StepResult.done(f"Installed {extra} dependencies")
    except subprocess.CalledProcessError as e:
        return StepResult.error(f"pip install secure-agents[{extra}] failed: {e}")


# ── Ollama service ───────────────────────────────────────────────────────────

def ensure_ollama_running() -> StepResult:
    """Start Ollama as a background service if not already running."""
    import httpx
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        if resp.status_code == 200:
            return StepResult.ok("Ollama is running")
    except Exception:
        pass

    # Try brew services first (background daemon)
    brew = shutil.which("brew")
    if brew:
        try:
            subprocess.run(
                [brew, "services", "start", "ollama"],
                check=True, capture_output=True, timeout=15,
            )
            # Wait for it to be ready
            import time
            for _ in range(10):
                time.sleep(1)
                try:
                    resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
                    if resp.status_code == 200:
                        return StepResult.done("Started Ollama via brew services")
                except Exception:
                    continue
        except Exception:
            pass

    # Fallback: popen
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        try:
            subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import time
            time.sleep(3)
            return StepResult.done("Started Ollama (background process)")
        except Exception as e:
            return StepResult.error(f"Failed to start Ollama: {e}")

    return StepResult.error("Ollama binary not found")


def ensure_ollama_model(model: str) -> StepResult:
    """Pull an Ollama model if not already available."""
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return StepResult.error("Ollama not installed")

    # Check if model already pulled
    try:
        result = subprocess.run(
            [ollama_bin, "list"],
            capture_output=True, text=True, timeout=10,
        )
        if model in result.stdout:
            return StepResult.ok(f"Model '{model}' already available")
    except Exception:
        pass

    # Pull it
    try:
        print(f"    Pulling model '{model}' (this may take several minutes)...")
        subprocess.run(
            [ollama_bin, "pull", model],
            check=True, timeout=600,
        )
        return StepResult.done(f"Pulled model '{model}'")
    except subprocess.TimeoutExpired:
        return StepResult.error(f"Model pull timed out after 10 minutes. Run manually: ollama pull {model}")
    except subprocess.CalledProcessError as e:
        return StepResult.error(f"ollama pull {model} failed: {e}")


# ── Config checks ────────────────────────────────────────────────────────────

def check_config_value(config_yaml_path: Path, key_path: str, sentinel: str) -> str | None:
    """Read a dotted key path from config.yaml. Returns None if missing or sentinel."""
    import yaml as _yaml
    if not config_yaml_path.exists():
        return None
    with open(config_yaml_path) as f:
        raw = _yaml.safe_load(f) or {}
    keys = key_path.split(".")
    node = raw
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    value = str(node) if node is not None else None
    if value == sentinel:
        return None
    return value


def update_config_value(config_yaml_path: Path, key_path: str, value: str) -> StepResult:
    """Update a value in config.yaml. Reuses the server's YAML writer."""
    try:
        # Import the server's writer to stay DRY
        sys.path.insert(0, str(config_yaml_path.parent))
        from secure_agents.ui.server import _update_yaml_value
        # Temporarily override the module-level config path
        import secure_agents.ui.server as _srv
        old_path = _srv._config_path
        _srv._config_path = str(config_yaml_path)
        try:
            _update_yaml_value(key_path, value)
        finally:
            _srv._config_path = old_path
        return StepResult.done(f"Set {key_path} = {value}")
    except Exception as e:
        return StepResult.error(f"Failed to update {key_path}: {e}")


# ── Credentials ──────────────────────────────────────────────────────────────

def check_credential(key: str) -> bool:
    """Check if a credential exists in keychain or env."""
    from secure_agents.core.credentials import get_credential
    return get_credential(key) is not None


def store_credential_value(key: str, value: str) -> StepResult:
    """Store a credential in the macOS Keychain."""
    from secure_agents.core.credentials import store_credential
    if store_credential(key, value):
        return StepResult.done(f"Stored '{key}' in Keychain")
    return StepResult.error(f"Failed to store '{key}'. Set {key.upper()} env var instead.")


def check_oauth2_token(username: str) -> bool:
    """Check if an OAuth2 token exists for the given username."""
    from secure_agents.core.credentials import get_oauth2_token
    return get_oauth2_token(username) is not None


def run_oauth2_flow(client_secrets: str, username: str) -> StepResult:
    """Run the Gmail OAuth2 authorization flow."""
    from secure_agents.core.credentials import run_oauth2_flow as _run
    if _run(client_secrets, username):
        return StepResult.done(f"OAuth2 authorized for {username}")
    return StepResult.error("OAuth2 flow failed. Check the error above.")


# ── Directories ──────────────────────────────────────────────────────────────

def ensure_directory(project_root: Path, dirname: str) -> StepResult:
    """Create a directory if it doesn't exist."""
    target = project_root / dirname
    if target.exists():
        return StepResult.ok(f"Directory '{dirname}' exists")
    target.mkdir(parents=True, exist_ok=True)
    return StepResult.done(f"Created '{dirname}'")


# ── Config file bootstrap ────────────────────────────────────────────────────

def ensure_config_yaml(project_root: Path) -> StepResult:
    """Copy config.example.yaml -> config.yaml if missing."""
    target = project_root / "config.yaml"
    if target.exists():
        return StepResult.ok("config.yaml exists")
    example = project_root / "config.example.yaml"
    if example.exists():
        shutil.copy2(example, target)
        return StepResult.done("Created config.yaml from example")
    return StepResult.error("No config.example.yaml found")
