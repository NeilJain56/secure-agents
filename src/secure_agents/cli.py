"""CLI for Secure Agents - start, list, validate, and manage credentials."""

from __future__ import annotations

import signal
import threading
from pathlib import Path

import click
import structlog

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


def _run_agents(agent_names: list[str], config) -> None:
    """Run one or more agents in parallel with coordinated shutdown."""
    agents = []
    for name in agent_names:
        agents.append((name, _build_agent(name, config)))

    if len(agents) == 1:
        name, agent = agents[0]
        click.echo(f"Starting agent: {name}")

        def _signal_handler(sig, frame):
            click.echo(f"\nStopping {name}...")
            agent.request_stop()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        agent.run()
        return

    # Multiple agents: run each in its own thread
    click.echo(f"Starting {len(agents)} agents in parallel: {', '.join(n for n, _ in agents)}")
    threads = []
    for name, agent in agents:
        t = threading.Thread(target=agent.run, name=f"agent-{name}", daemon=True)
        t.start()
        threads.append((name, agent, t))

    # Main thread waits for SIGINT/SIGTERM then stops all agents
    shutdown = threading.Event()

    def _signal_handler(sig, frame):
        click.echo(f"\nStopping {len(agents)} agents...")
        for name, agent, _ in threads:
            agent.request_stop()
        shutdown.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Wait for all threads or a shutdown signal
    try:
        while not shutdown.is_set():
            alive = [t for _, _, t in threads if t.is_alive()]
            if not alive:
                break
            shutdown.wait(timeout=1.0)
    except KeyboardInterrupt:
        for name, agent, _ in threads:
            agent.request_stop()

    # Give threads a moment to finish
    for _, _, t in threads:
        t.join(timeout=5.0)

    click.echo("All agents stopped.")


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

    if agent_names:
        names = list(agent_names)
    else:
        # Start all enabled agents
        names = [
            name for name, agent_cfg in config.agents.items()
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
def list_plugins():
    """List all registered agents, tools, and providers."""
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
    """Interactively store credentials in the macOS Keychain."""
    from secure_agents.core.credentials import store_credential

    click.echo("Store credentials securely in the macOS Keychain.")
    click.echo("Press Enter to skip any credential you don't need.\n")

    pairs = [
        ("email_password", "Email password / Gmail App Password"),
        ("anthropic_api_key", "Anthropic API key"),
        ("openai_api_key", "OpenAI API key"),
        ("gemini_api_key", "Gemini API key"),
    ]
    for key, label in pairs:
        value = click.prompt(f"  {label}", default="", hide_input=True, show_default=False)
        if value:
            if store_credential(key, value):
                click.echo(f"    Stored '{key}' in Keychain")
            else:
                click.echo(f"    Failed to store '{key}' - set {key.upper()} env var instead")

    click.echo("\nDone. Credentials are stored in the macOS Keychain under 'secure-agents'.")


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
        active = config.provider.active
        try:
            provider_cls = registry.get_provider(active)
            settings = getattr(config.provider, active)
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

    # Show configured agents
    if config:
        for name, agent_cfg in config.agents.items():
            enabled = agent_cfg.get("enabled", True)
            status = "enabled" if enabled else "disabled"
            click.echo(f"  - {name}: {status}")

    # Check optional dependencies
    for pkg, label in [("anthropic", "Anthropic"), ("openai", "OpenAI"), ("google.genai", "Gemini")]:
        try:
            __import__(pkg)
            click.echo(f"[OK] {label} SDK installed")
        except ImportError:
            warnings.append(f"{label} SDK not installed (optional)")

    # Check Docker
    try:
        import docker
        client = docker.from_env()
        client.ping()
        click.echo("[OK] Docker is available")
    except Exception:
        warnings.append("Docker not available (sandbox will use subprocess fallback)")

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
