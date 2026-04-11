"""Orchestrator: checks what's needed, skips what's satisfied, only acts on gaps."""

from __future__ import annotations

from pathlib import Path

import click

from secure_agents.setup.manifest import SetupPlan
from secure_agents.setup import steps


# ── Colored output helpers ────────────────────────────────────────────────────

def _status(result: steps.StepResult) -> None:
    icons = {
        "ok": click.style("  [OK]    ", fg="green"),
        "done": click.style("  [DONE]  ", fg="green", bold=True),
        "skipped": click.style("  [SKIP]  ", fg="yellow"),
        "error": click.style("  [ERROR] ", fg="red", bold=True),
    }
    click.echo(f"{icons.get(result.status, '  [???]  ')} {result.message}")


def _header(title: str) -> None:
    click.echo()
    click.echo(click.style(f"  --- {title} ---", fg="cyan", bold=True))


# ── Pre-check: figure out what's already satisfied ───────────────────────────

def _check_state(plan: SetupPlan, project_root: Path) -> dict:
    """Scan current system state and return what's missing."""
    import shutil
    import subprocess
    config_path = project_root / "config.yaml"

    state: dict = {
        "config_exists": config_path.exists(),
        "brew_needed": [],
        "pip_needed": [],
        "ollama_running": False,
        "model_needed": None,
        "config_missing": [],
        "creds_missing": [],
        "dirs_missing": [],
        "auth_method": plan.auth_method,
        "email_username": plan.email_username,
    }

    # Homebrew packages
    for pkg in plan.homebrew_packages:
        if not shutil.which(pkg):
            state["brew_needed"].append(pkg)

    # Pip extras
    venv_pip = project_root / ".venv" / "bin" / "pip"
    pip_cmd = str(venv_pip) if venv_pip.exists() else "pip"
    check_packages = {
        "gmail-oauth": "google-auth-oauthlib",
    }
    for extra in plan.pip_extras:
        check_pkg = check_packages.get(extra)
        if check_pkg:
            result = subprocess.run(
                [pip_cmd, "show", check_pkg],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                state["pip_needed"].append(extra)
        else:
            state["pip_needed"].append(extra)

    # Ollama
    for action in plan.post_install:
        if action.action == "start_service" and action.service == "ollama":
            try:
                import httpx
                resp = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
                state["ollama_running"] = resp.status_code == 200
            except Exception:
                state["ollama_running"] = False

        elif action.action == "pull_model":
            model = action.default
            configured = steps.check_config_value(config_path, action.model_key, "")
            if configured:
                model = configured
            ollama_bin = shutil.which("ollama")
            if ollama_bin:
                try:
                    result = subprocess.run(
                        [ollama_bin, "list"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if model not in result.stdout:
                        state["model_needed"] = model
                except Exception:
                    state["model_needed"] = model
            else:
                state["model_needed"] = model

    # Config values
    for check in plan.config_checks:
        current = steps.check_config_value(config_path, check.path, check.sentinel)
        if not current:
            state["config_missing"].append(check)

    # Credentials
    for cred in plan.credentials:
        if cred.condition and "==" in cred.condition:
            _, expected = cred.condition.split("==", 1)
            if plan.auth_method != expected.strip():
                continue
        if cred.flow == "gmail_oauth2":
            username = plan.email_username
            if username and username != "your-email@gmail.com":
                if not steps.check_oauth2_token(username):
                    state["creds_missing"].append(cred)
            else:
                state["creds_missing"].append(cred)
        else:
            if not steps.check_credential(cred.key):
                state["creds_missing"].append(cred)

    # Directories
    for dirname in plan.directories:
        if not (project_root / dirname).exists():
            state["dirs_missing"].append(dirname)

    return state


# ── Main runner ──────────────────────────────────────────────────────────────

def run_plan(
    plan: SetupPlan,
    project_root: Path,
    dry_run: bool = False,
    skip_dashboard: bool = False,
) -> bool:
    """Check what's needed, skip what's satisfied, act only on gaps."""
    config_path = project_root / "config.yaml"
    errors: list[str] = []

    agents_str = ", ".join(plan.agent_names)
    click.echo()
    click.echo(click.style("=================================================", bold=True))
    click.echo(click.style("  Secure Agents - Setup", bold=True))
    click.echo(click.style("=================================================", bold=True))
    click.echo(f"  Agents:   {agents_str}")
    click.echo(f"  Provider: {plan.provider_name}")

    # ── Pre-flight check ──────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Checking requirements...", dim=True))
    state = _check_state(plan, project_root)

    needs_work = (
        not state["config_exists"]
        or state["brew_needed"]
        or state["pip_needed"]
        or state["config_missing"]
        or state["creds_missing"]
        or state["dirs_missing"]
        or state["model_needed"]
        or (any(a.action == "start_service" for a in plan.post_install)
            and not state["ollama_running"])
    )

    if not needs_work:
        click.echo()
        click.echo(click.style("  All requirements already met!", fg="green", bold=True))
        _print_all_ok(plan, state, project_root)
        if not skip_dashboard:
            _offer_dashboard(config_path)
        return True

    if dry_run:
        _print_dry_run(plan, state, project_root)
        return True

    # ── Only run sections that have gaps ──────────────────────────────

    # 1. Config file
    if not state["config_exists"]:
        _header("Configuration")
        r = steps.ensure_config_yaml(project_root)
        _status(r)
        if r.status == "error":
            errors.append(r.message)

    # 2. Homebrew packages (only missing ones)
    if state["brew_needed"]:
        _header("System Packages")
        r = steps.ensure_homebrew()
        _status(r)
        if r.status == "error":
            errors.append(r.message)
        else:
            for pkg in state["brew_needed"]:
                r = steps.ensure_homebrew_package(pkg)
                _status(r)
                if r.status == "error":
                    errors.append(r.message)

    # 3. Pip extras (only missing ones)
    if state["pip_needed"]:
        _header("Python Dependencies")
        for extra in state["pip_needed"]:
            r = steps.ensure_pip_extra(extra, project_root)
            _status(r)
            if r.status == "error":
                errors.append(r.message)

    # 4. Ollama service (only if not already running)
    needs_ollama_start = (
        any(a.action == "start_service" for a in plan.post_install)
        and not state["ollama_running"]
    )
    if needs_ollama_start:
        _header("Ollama Service")
        r = steps.ensure_ollama_running()
        _status(r)
        if r.status == "error":
            errors.append(r.message)

    # 5. Model pull (only if model not present)
    if state["model_needed"]:
        _header("LLM Model")
        r = steps.ensure_ollama_model(state["model_needed"])
        _status(r)
        if r.status == "error":
            errors.append(r.message)

    # 6. Config values (only missing ones)
    if state["config_missing"]:
        _header("Email Configuration")
        for check in state["config_missing"]:
            value = click.prompt(f"    {check.prompt}", default="")
            if value:
                r = steps.update_config_value(config_path, check.path, value)
                _status(r)
                if r.status == "error":
                    errors.append(r.message)
                if "username" in check.path:
                    plan.email_username = value
            else:
                _status(steps.StepResult.skipped(f"Skipped {check.prompt}"))

    # 7. Auth method (only ask if email creds are missing)
    if state["creds_missing"]:
        email_creds_missing = [c for c in state["creds_missing"]
                               if c.key in ("email_password", "oauth2")]
        if email_creds_missing:
            click.echo()
            current_method = plan.auth_method
            click.echo(f"    Current email auth method: {click.style(current_method, bold=True)}")
            if click.confirm("    Change auth method?", default=False):
                choice = click.prompt(
                    "    Auth method",
                    type=click.Choice(["app_password", "oauth2"]),
                    default=current_method,
                )
                if choice != current_method:
                    steps.update_config_value(config_path, "defaults.email.imap.auth_method", choice)
                    steps.update_config_value(config_path, "defaults.email.smtp.auth_method", choice)
                    plan.auth_method = choice
                    _status(steps.StepResult.done(f"Auth method set to {choice}"))
                    # Re-check which creds are actually needed with new auth method
                    state = _check_state(plan, project_root)

    # 8. Credentials (only missing ones)
    if state["creds_missing"]:
        _header("Credentials")
        for cred in state["creds_missing"]:
            # Re-evaluate condition with possibly updated auth_method
            if cred.condition and "==" in cred.condition:
                _, expected = cred.condition.split("==", 1)
                if plan.auth_method != expected.strip():
                    continue

            if cred.flow == "gmail_oauth2":
                username = plan.email_username
                if not username or username == "your-email@gmail.com":
                    _status(steps.StepResult.skipped(
                        "OAuth2: email username not set yet. Configure email first."
                    ))
                    continue
                if steps.check_oauth2_token(username):
                    _status(steps.StepResult.ok(f"OAuth2 token exists for {username}"))
                    continue
                click.echo(f"    Gmail OAuth2 authorization needed for {username}")
                secrets_path = click.prompt(
                    "    Path to client_secrets.json",
                    type=click.Path(exists=True),
                )
                r = steps.run_oauth2_flow(secrets_path, username)
                _status(r)
                if r.status == "error":
                    errors.append(r.message)
            else:
                value = click.prompt(
                    f"    {cred.label or cred.key}",
                    default="",
                    hide_input=cred.hide_input,
                    show_default=False,
                )
                if value:
                    r = steps.store_credential_value(cred.key, value)
                    _status(r)
                    if r.status == "error":
                        errors.append(r.message)
                else:
                    _status(steps.StepResult.skipped(f"Skipped {cred.key}"))

    # 9. Directories (only missing ones)
    if state["dirs_missing"]:
        _header("Directories")
        for dirname in state["dirs_missing"]:
            r = steps.ensure_directory(project_root, dirname)
            _status(r)

    # ── Summary ───────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("=================================================", bold=True))
    if errors:
        click.echo(click.style(f"  Setup completed with {len(errors)} error(s)", fg="yellow", bold=True))
        click.echo(click.style("=================================================", bold=True))
        for err in errors:
            click.echo(click.style(f"  ! {err}", fg="red"))
        click.echo()
    else:
        click.echo(click.style("  Setup complete!", fg="green", bold=True))
        click.echo(click.style("=================================================", bold=True))

    click.echo()
    click.echo("  Next steps:")
    if not skip_dashboard:
        click.echo("    The dashboard will open at http://127.0.0.1:8420")
    click.echo(f"    Start agents: secure-agents start {' '.join(plan.agent_names)}")
    click.echo(f"    Validate:     secure-agents validate")
    click.echo()

    if not skip_dashboard and not errors:
        _offer_dashboard(config_path)

    return len(errors) == 0


# ── Output helpers ────────────────────────────────────────────────────────────

def _print_all_ok(plan: SetupPlan, state: dict, project_root: Path) -> None:
    """Show a summary when everything is already satisfied."""
    click.echo()
    items = []
    if plan.homebrew_packages:
        items.append(f"System packages: {', '.join(plan.homebrew_packages)}")
    if plan.pip_extras:
        items.append(f"Python extras: {', '.join(plan.pip_extras)}")
    if any(a.action == "start_service" for a in plan.post_install):
        items.append("Ollama service: running")
    if any(a.action == "pull_model" for a in plan.post_install):
        items.append("LLM model: available")
    if plan.config_checks:
        items.append("Email config: set")
    for cred in plan.credentials:
        if cred.condition and "==" in cred.condition:
            _, expected = cred.condition.split("==", 1)
            if plan.auth_method != expected.strip():
                continue
        items.append(f"Credential ({cred.key}): found")
    if plan.directories:
        items.append(f"Directories: {', '.join(plan.directories)}")

    for item in items:
        click.echo(click.style(f"  [OK]    ", fg="green") + item)
    click.echo()


def _print_dry_run(plan: SetupPlan, state: dict, project_root: Path) -> None:
    """Show only what's actually missing -- not the full plan."""
    click.echo()
    click.echo(click.style("  DRY RUN - showing only what needs to be done", fg="yellow", bold=True))
    click.echo()

    anything = False

    if not state["config_exists"]:
        click.echo("  Will create config.yaml from example")
        anything = True

    if state["brew_needed"]:
        click.echo("  Homebrew packages to install:")
        for pkg in state["brew_needed"]:
            click.echo(f"    - {pkg}")
        anything = True

    if state["pip_needed"]:
        click.echo("  Python dependencies to install:")
        for extra in state["pip_needed"]:
            click.echo(f"    - secure-agents[{extra}]")
        anything = True

    needs_ollama_start = (
        any(a.action == "start_service" for a in plan.post_install)
        and not state["ollama_running"]
    )
    if needs_ollama_start:
        click.echo("  Will start Ollama service")
        anything = True

    if state["model_needed"]:
        click.echo(f"  Will pull model: {state['model_needed']}")
        anything = True

    if state["config_missing"]:
        click.echo("  Config values to set:")
        for check in state["config_missing"]:
            click.echo(f"    - {check.path}: {check.prompt}")
        anything = True

    if state["creds_missing"]:
        click.echo("  Credentials to configure:")
        for cred in state["creds_missing"]:
            click.echo(f"    - {cred.label or cred.key}")
        anything = True

    if state["dirs_missing"]:
        click.echo("  Directories to create:")
        for d in state["dirs_missing"]:
            click.echo(f"    - {project_root / d}")
        anything = True

    if not anything:
        click.echo(click.style("  Nothing to do -- all requirements met!", fg="green"))

    click.echo()


def _offer_dashboard(config_path: Path) -> None:
    if click.confirm("  Launch the web dashboard now?", default=True):
        from secure_agents.ui.server import run_server
        run_server(
            config_path=str(config_path),
            host="127.0.0.1",
            port=8420,
            open_browser=True,
        )
