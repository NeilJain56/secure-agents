"""CLI for Secure Agents - start, list, validate, and manage credentials."""

from __future__ import annotations

import select
import signal
import sys
import threading
import time
from pathlib import Path

import click
import structlog

from secure_agents.core.agent_status import (
    clear_gate,
    clear_pipeline_status,
    clear_status,
    consume_gate_approval,
    write_gate,
    write_gate_approval,
    write_pipeline_status,
    write_status,
)
from secure_agents.core.builder import build_agent, discover_all
from secure_agents.core.config import load_config
from secure_agents.core.logger import setup_logging
from secure_agents.core.registry import registry

logger = structlog.get_logger()


def _build_agent(agent_name: str, config):
    """Instantiate an agent, raising ClickException if disabled."""
    merged = config.get_agent_config(agent_name)
    if not merged.get("enabled", True):
        raise click.ClickException(f"Agent '{agent_name}' is disabled in config.")
    return build_agent(agent_name, config)


def _run_agents(agent_names: list[str], config, abort: threading.Event | None = None) -> bool:
    """Run one or more agents in parallel; block until all finish or *abort* fires.

    Returns True if all agents completed normally, False if interrupted.
    When *abort* is None the function installs its own SIGINT/SIGTERM handlers.
    """
    agents = [(name, _build_agent(name, config)) for name in agent_names]

    if len(agents) == 1:
        name, agent = agents[0]
        click.echo(f"  Starting {name}")
        _abort = abort or threading.Event()

        if abort is None:
            def _signal_handler(sig, frame):
                click.echo(f"\nStopping {name}...")
                agent.request_stop()
                clear_status(name)
                _abort.set()
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)

        write_status(name)
        try:
            agent.run()
        finally:
            clear_status(name)
        return not _abort.is_set()

    # Multiple agents: run each in its own thread
    click.echo(f"  Starting {', '.join(n for n, _ in agents)} in parallel")
    threads = []
    for name, agent in agents:
        write_status(name)

        def _make_target(a, n):
            def _run():
                try:
                    a.run()
                finally:
                    clear_status(n)
            return _run

        t = threading.Thread(target=_make_target(agent, name), name=f"agent-{name}", daemon=True)
        t.start()
        threads.append((name, agent, t))

    _abort = abort or threading.Event()

    if abort is None:
        def _signal_handler(sig, frame):
            click.echo(f"\nStopping agents...")
            for n, a, _ in threads:
                a.request_stop()
                clear_status(n)
            _abort.set()
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    try:
        while not _abort.is_set():
            alive = [t for _, _, t in threads if t.is_alive()]
            if not alive:
                break
            _abort.wait(timeout=1.0)
    except KeyboardInterrupt:
        for n, a, _ in threads:
            a.request_stop()
            clear_status(n)
        _abort.set()

    if _abort.is_set():
        for n, a, _ in threads:
            a.request_stop()
            clear_status(n)
        for _, _, t in threads:
            t.join(timeout=5.0)
        return False

    for _, _, t in threads:
        t.join(timeout=5.0)
    return True


def _handle_confirm_gate(
    pipeline_name: str, message: str, abort: threading.Event
) -> bool:
    """Pause the pipeline and wait for human approval — terminal or dashboard.

    Writes a gate status file so the dashboard can surface a confirmation
    prompt.  Polls simultaneously for:
      - A line typed in the terminal (y/yes/Enter = proceed; anything else = stop)
      - An approval file written by the dashboard's approve/reject button
      - The shared abort event (Ctrl-C)

    Returns True to proceed, False to stop.
    """
    write_gate(pipeline_name, message)

    sep = "─" * 60
    click.echo(f"\n{sep}")
    click.echo(f"⏸  {message}")
    click.echo(f"   Type [y] + Enter to continue, [n] + Enter to stop.")
    click.echo(f"   Or use the dashboard to approve / reject.")
    click.echo(f"{sep}\n")
    sys.stdout.flush()

    try:
        while not abort.is_set():
            # Check for dashboard-driven approval
            decision = consume_gate_approval(pipeline_name)
            if decision is not None:
                if decision:
                    click.echo("✓  Approved via dashboard. Continuing...\n")
                else:
                    click.echo("✗  Rejected via dashboard. Pipeline stopped.\n")
                return decision

            # Check stdin (non-blocking, only on interactive terminals)
            if sys.stdin.isatty():
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0.4)
                    if r:
                        line = sys.stdin.readline().strip().lower()
                        proceed = line in ("y", "yes", "")
                        if proceed:
                            click.echo("Continuing...\n")
                        else:
                            click.echo("Pipeline stopped.\n")
                        return proceed
                except (OSError, ValueError):
                    pass
            else:
                # Non-interactive — only watch for file-based approval
                abort.wait(timeout=0.4)

        return False
    finally:
        clear_gate(pipeline_name)


def _run_pipeline(pipeline_name: str, pipeline_cfg: dict, config) -> None:
    """Run a pipeline stage-by-stage.

    Each stage's agents run in parallel.  The next stage only starts after
    every agent in the current stage has finished.  Ctrl-C stops the active
    stage and does not proceed to the next one.

    If the pipeline has no ``stages`` list, all agents run in parallel at
    once (same as calling _run_agents directly).
    """
    stages = pipeline_cfg.get("stages")
    if not stages:
        agent_names = pipeline_cfg.get("agents", [])
        _run_agents(agent_names, config)
        return

    desc = pipeline_cfg.get("description", "")

    # Build summary, skipping confirm gates for the stage count / label
    agent_stages = [s for s in stages if isinstance(s, list)]
    stage_summary = " → ".join(
        f"[{', '.join(s)}]" if len(s) > 1 else s[0]
        for s in agent_stages
    )
    click.echo(f"Pipeline '{pipeline_name}'" + (f" — {desc}" if desc else ""))
    click.echo(
        f"  {len(agent_stages)} stage{'s' if len(agent_stages) != 1 else ''}: "
        f"{stage_summary}\n"
    )

    abort = threading.Event()

    def _signal_handler(sig, frame):
        click.echo(f"\nAborting pipeline '{pipeline_name}'...")
        # If a gate is waiting for approval, write a rejection so the polling
        # loop in _handle_confirm_gate exits cleanly.
        write_gate_approval(pipeline_name, False)
        abort.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    write_pipeline_status(pipeline_name)
    agent_stage_idx = 0  # counts only agent stages for display
    try:
        for stage in stages:
            if abort.is_set():
                break

            # ── Confirm gate ──────────────────────────────────────────────
            if isinstance(stage, dict) and "confirm" in stage:
                ok = _handle_confirm_gate(pipeline_name, stage["confirm"], abort)
                if not ok:
                    click.echo("Pipeline stopped at confirmation gate.")
                    return
                continue

            # ── Agent stage ───────────────────────────────────────────────
            agent_stage_idx += 1
            total_agent_stages = len(agent_stages)
            parallel_note = " (parallel)" if len(stage) > 1 else ""
            click.echo(
                f"Stage {agent_stage_idx}/{total_agent_stages}{parallel_note}: "
                f"{', '.join(stage)}"
            )
            ok = _run_agents(stage, config, abort=abort)
            if not ok:
                click.echo(f"\nPipeline aborted at stage {agent_stage_idx}.")
                return
            click.echo(f"Stage {agent_stage_idx} complete.\n")
    finally:
        clear_pipeline_status(pipeline_name)

    if not abort.is_set():
        click.echo(f"Pipeline '{pipeline_name}' complete.")


@click.group()
@click.option("--config", "-c", default="config.yaml", help="Path to config file")
@click.option("--json-logs", is_flag=True, help="Output logs as JSON")
@click.pass_context
def main(ctx, config, json_logs):
    """Secure Agents - Secure on-prem AI agent framework."""
    setup_logging(json_output=json_logs)
    discover_all()
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    # Configure the credential backend from the loaded config so every
    # subcommand reads/writes secrets from the right place.  We do not
    # fail here if the config is missing — `secure-agents setup` needs
    # to run before there is a config file.
    try:
        from secure_agents.core.credentials import configure_credentials
        cfg = load_config(config)
        configure_credentials(
            backend=cfg.credentials.backend,
            store_path=cfg.credentials.store_path,
        )
    except Exception:
        pass

    # Initialize persistent metrics store so CLI-started agents persist tick
    # data to SQLite — mirrors what server.py does at dashboard startup.
    try:
        from secure_agents.core.metrics import metrics
        from secure_agents.core.metrics_store import get_store
        db_path = Path(config).resolve().parent / "data" / "metrics.db"
        _store = get_store(str(db_path))
        metrics.set_store(_store)
    except Exception:
        pass


@main.command()
@click.argument("agent_names", nargs=-1)
@click.pass_context
def start(ctx, agent_names):
    """Start agents in parallel.

    Specify agent names to start specific agents, or omit to start all enabled agents.

    Examples:

        secure-agents start                       # all enabled agents
        secure-agents start nda_reviewer           # just one
        secure-agents start nda_reviewer contract_analyzer  # specific set
    """
    config = load_config(ctx.obj["config_path"])
    pipelines = getattr(config, "pipelines", {}) or {}

    if agent_names:
        raw_names = list(agent_names)

        # Single pipeline name → run stage-by-stage
        if len(raw_names) == 1 and raw_names[0] in pipelines:
            name = raw_names[0]
            _run_pipeline(name, pipelines[name], config)
            return

        # Mix of pipeline names and individual agents → expand flat, run in parallel
        names: list[str] = []
        for name in raw_names:
            if name in pipelines:
                names.extend(pipelines[name].get("agents", []))
            else:
                names.append(name)
        # De-duplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        names = deduped
    else:
        # Start all enabled agents
        names = [
            n for n, agent_cfg in config.agents.items()
            if agent_cfg.get("enabled", True)
        ]

    if not names:
        raise click.ClickException(
            "No agents to start. Add agents to config.yaml or specify names."
        )

    # Validate all agents exist before starting any
    for name in names:
        if name not in config.agents:
            raise click.ClickException(
                f"Agent '{name}' not found in config. "
                f"Available: {list(config.agents.keys())}"
            )
        try:
            registry.get_agent(name)
        except KeyError:
            raise click.ClickException(
                f"Agent '{name}' is configured but has no registered implementation. "
                f"Registered agents: {registry.list_agents()}"
            )

    _run_agents(names, config)


@main.command(name="list")
@click.pass_context
def list_plugins(ctx):
    """List all registered agents, tools, providers, and pipelines."""
    try:
        config = load_config(ctx.obj["config_path"])
        pipelines = getattr(config, "pipelines", {}) or {}
    except Exception:
        pipelines = {}

    if pipelines:
        click.echo("Pipelines:")
        for name, pcfg in pipelines.items():
            desc = pcfg.get("description", "")
            agents = pcfg.get("agents", [])
            click.echo(f"  - {name}" + (f": {desc}" if desc else ""))
            click.echo(f"    agents: {' → '.join(agents)}")
        click.echo()

    click.echo("Agents:")
    for name in registry.list_agents():
        cls = registry.get_agent(name)
        click.echo(f"  - {name}: {cls.description}")

    click.echo("\nTools:")
    for name in registry.list_tools():
        cls = registry.get_tool_class(name)
        click.echo(f"  - {name}: {cls.description}")

    click.echo("\nProviders:")
    for name in registry.list_providers():
        click.echo(f"  - {name}")


@main.group()
def auth():
    """Manage credentials (API keys, email passwords, OAuth2)."""
    pass


@auth.command(name="setup")
def auth_setup():
    """Interactively store credentials in the active credential backend."""
    from secure_agents.core.credentials import get_active_backend, store_credential

    backend = get_active_backend()
    click.echo(f"Storing credentials in backend: {backend.name}")
    if backend.name == "encrypted_file":
        click.echo(
            "  (You will be prompted for the master passphrase, or set "
            "SECURE_AGENTS_MASTER_KEY in your environment.)"
        )
    click.echo("Press Enter to skip any credential you don't need.\n")

    pairs = [
        ("email_password", "Email password / Gmail App Password"),
    ]
    for key, label in pairs:
        value = click.prompt(f"  {label}", default="", hide_input=True, show_default=False)
        if value:
            if store_credential(key, value):
                click.echo(f"    Stored '{key}' in {backend.name}")
            else:
                click.echo(
                    f"    Failed to store '{key}' in {backend.name}. "
                    f"Set {key.upper()} env var instead, or run "
                    f"`secure-agents auth init-store` first."
                )

    click.echo(f"\nDone. Credentials are stored via the '{backend.name}' backend.")


@auth.command(name="backend")
@click.pass_context
def auth_backend(ctx):
    """Show which credential backend is active and what it can find."""
    from secure_agents.core.credential_backends import EncryptedFileBackend
    from secure_agents.core.credentials import get_active_backend

    backend = get_active_backend()
    click.echo(f"Active backend: {backend.name}")
    if isinstance(backend, EncryptedFileBackend):
        click.echo(f"  Store path:    {backend.store_path}")
        if backend.store_path.exists():
            mode = backend.store_path.stat().st_mode & 0o777
            click.echo(f"  Permissions:  {oct(mode)}")
            try:
                keys = backend.list_keys()
                click.echo(f"  Stored keys:  {', '.join(keys) if keys else '(none)'}")
            except Exception as e:
                click.echo(f"  Stored keys:  (locked: {e})")
        else:
            click.echo("  Store status: not initialized — run `secure-agents auth init-store`")


@auth.command(name="init-store")
@click.option("--store-path", default=None,
              help="Override the encrypted store path (defaults to credentials.store_path)")
@click.option("--from-env", is_flag=True,
              help="Read the master passphrase from SECURE_AGENTS_MASTER_KEY instead of prompting")
@click.pass_context
def auth_init_store(ctx, store_path, from_env):
    """Initialize an encrypted credential store with a fresh master passphrase.

    Use this on Linux VMs and headless servers where the macOS Keychain is not
    available.  Pick a strong passphrase — anyone with the passphrase AND the
    encrypted file can read every secret in the store.
    """
    import os as _os

    from secure_agents.core.credential_backends import (
        MASTER_KEY_ENV,
        MIN_PASSPHRASE_LEN,
        EncryptedFileBackend,
    )

    config = load_config(ctx.obj.get("config_path", "config.yaml"))
    path = store_path or config.credentials.store_path
    backend = EncryptedFileBackend(path)

    if backend.store_path.exists():
        raise click.ClickException(
            f"Refusing to overwrite existing store at {backend.store_path}.  "
            f"Delete it manually if you really mean to start over."
        )

    if from_env:
        passphrase = _os.environ.get(MASTER_KEY_ENV)
        if not passphrase:
            raise click.ClickException(
                f"--from-env requested but {MASTER_KEY_ENV} is not set in the environment."
            )
    else:
        click.echo(
            f"Initializing encrypted credential store at {backend.store_path}\n"
            f"Choose a strong passphrase (>= {MIN_PASSPHRASE_LEN} characters)."
        )
        passphrase = click.prompt(
            "Master passphrase", hide_input=True, confirmation_prompt=True,
        )

    try:
        ok = backend.initialize(passphrase)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not ok:
        raise click.ClickException(
            f"Failed to initialize store at {backend.store_path}."
        )
    click.echo(f"Encrypted credential store created at {backend.store_path} (mode 0600).")
    click.echo(
        f"Tip: export {MASTER_KEY_ENV}=... in your shell or systemd unit "
        f"to unlock the store non-interactively."
    )


@auth.command(name="gmail")
@click.argument("client_secrets", type=click.Path(exists=True))
@click.pass_context
def auth_gmail(ctx, client_secrets):
    """Set up Gmail OAuth2 authentication.

    Requires a client_secrets.json from Google Cloud Console.
    Opens a browser for you to authorize access.
    """
    from secure_agents.core.credentials import run_oauth2_flow
    config = load_config(ctx.obj.get("config_path", "config.yaml"))
    # Pull username from defaults
    username = config.defaults.get("email", {}).get("imap", {}).get("username", "")
    if not username:
        raise click.ClickException("Set defaults.email.imap.username in config.yaml first.")

    click.echo(f"Starting OAuth2 flow for: {username}")
    if run_oauth2_flow(client_secrets, username):
        click.echo("OAuth2 setup complete. Token stored in ~/.secure-agents/tokens/")
    else:
        raise click.ClickException("OAuth2 flow failed. Check the error above.")


main.add_command(auth)


@main.command()
@click.argument("agent_names", nargs=-1)
@click.option("--all", "setup_all", is_flag=True, help="Set up ALL agents, even disabled ones")
@click.option("--provider", "provider_override", default=None, help="Override which LLM provider to set up")
@click.option("--skip-dashboard", is_flag=True, help="Don't launch the dashboard when done")
@click.option("--dry-run", is_flag=True, help="Show what would be done without doing it")
@click.pass_context
def setup(ctx, agent_names, setup_all, provider_override, skip_dashboard, dry_run):
    """Set up everything needed to run selected agents.

    Reads each agent's tools and provider from config, resolves all
    dependencies (Homebrew packages, pip extras, credentials, OAuth2),
    and runs idempotent setup steps in the right order.

    \b
    Examples:
        secure-agents setup                       # all enabled agents
        secure-agents setup nda_reviewer           # just one agent
        secure-agents setup --provider anthropic   # force a provider
        secure-agents setup --dry-run              # preview what would happen
    """
    from secure_agents.setup.manifest import load_manifest, resolve_plan
    from secure_agents.setup.runner import run_plan

    project_root = Path(ctx.obj["config_path"]).resolve().parent
    config = load_config(ctx.obj["config_path"])

    # Determine which agents to set up
    if agent_names:
        names = list(agent_names)
    elif setup_all:
        names = list(config.agents.keys())
    else:
        names = [n for n, cfg in config.agents.items() if cfg.get("enabled", True)]

    if not names:
        # Fall back to all registered agents
        names = registry.list_agents()

    if not names:
        raise click.ClickException(
            "No agents found. Add agents to config.yaml or specify names."
        )

    # Load manifest and resolve the plan
    manifest = load_manifest(project_root)
    plan = resolve_plan(names, config, manifest, provider_override)

    # Run it
    success = run_plan(
        plan,
        project_root=project_root,
        dry_run=dry_run,
        skip_dashboard=skip_dashboard,
    )

    if not success:
        raise SystemExit(1)


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8420, type=int, help="Port to bind to")
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically")
@click.pass_context
def ui(ctx, host, port, no_browser):
    """Launch the web dashboard for managing agents."""
    from secure_agents.ui.server import run_server
    run_server(
        config_path=ctx.obj["config_path"],
        host=host,
        port=port,
        open_browser=not no_browser,
    )


@main.command()
@click.pass_context
def validate(ctx):
    """Validate configuration and check dependencies."""
    config_path = ctx.obj["config_path"]
    errors = []
    warnings = []

    # Check config file
    if not Path(config_path).exists():
        errors.append(f"Config file not found: {config_path}")
        errors.append("Run: cp config.example.yaml config.yaml")
    else:
        try:
            config = load_config(config_path)
        except Exception as e:
            errors.append(f"Config parse error: {e}")
            config = None

    # Check provider availability
    if config:
        active = config.active_provider
        try:
            provider_cls = registry.get_provider(active)
            if not getattr(provider_cls, "local_only", False):
                errors.append(f"Provider '{active}' is not declared local_only=True")
            settings = config.get_provider_settings(active)
            provider = provider_cls(settings.model_dump())
            if provider.is_available():
                click.echo(f"[OK] Provider '{active}' is available")
            else:
                warnings.append(f"Provider '{active}' is not reachable. Is it running?")
        except KeyError:
            errors.append(f"Provider '{active}' not registered")

    # Check registered plugins
    click.echo(f"[OK] {len(registry.list_agents())} agent(s) registered")
    click.echo(f"[OK] {len(registry.list_tools())} tool(s) registered")
    click.echo(f"[OK] {len(registry.list_providers())} provider(s) registered")

    # Check credential backend
    if config:
        from secure_agents.core.credential_backends import EncryptedFileBackend
        from secure_agents.core.credentials import get_active_backend
        backend = get_active_backend()
        click.echo(f"[OK] Credential backend: {backend.name}")
        if isinstance(backend, EncryptedFileBackend):
            if not backend.store_path.exists():
                warnings.append(
                    f"Encrypted credential store does not exist at "
                    f"{backend.store_path}. Run: secure-agents auth init-store"
                )
            else:
                mode = backend.store_path.stat().st_mode & 0o777
                if mode & 0o077:
                    errors.append(
                        f"Encrypted credential store {backend.store_path} has "
                        f"insecure permissions {oct(mode)}. Run: chmod 600 "
                        f"{backend.store_path}"
                    )

    # Show configured agents
    if config:
        for name, agent_cfg in config.agents.items():
            enabled = agent_cfg.get("enabled", True)
            status = "enabled" if enabled else "disabled"
            click.echo(f"  - {name}: {status}")

    # Check Docker (required for sandbox)
    try:
        import docker
        client = docker.from_env()
        client.ping()
        click.echo("[OK] Docker is available (sandbox ready)")
    except Exception:
        warnings.append(
            "Docker not available. Sandbox mode (enabled by default) requires Docker. "
            "Install Docker or set security.sandbox_enabled: false in config (NOT RECOMMENDED)."
        )

    # Report
    if warnings:
        click.echo(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            click.echo(f"  [WARN] {w}")

    if errors:
        click.echo(f"\n{len(errors)} error(s):")
        for e in errors:
            click.echo(f"  [ERROR] {e}")
        raise SystemExit(1)
    else:
        click.echo("\nValidation passed.")
