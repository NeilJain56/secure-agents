# CLAUDE.md - AI Development Guide

## Project Purpose

Secure Agents is a secure, on-prem AI agent framework for legal professionals and anyone handling sensitive data. Agents automate workflows (NDA review, contract analysis, compliance monitoring) using modular tools and pluggable local-only LLM providers (Ollama, llama.cpp, vLLM, LM Studio, LocalAI, or any OpenAI-compatible local server). No data ever leaves the machine — every provider must declare `local_only = True` and the builder enforces this at runtime. Defense-in-depth security: credentials never touch disk in plaintext (resolved through a pluggable backend — macOS Keychain on laptops, AES-256-GCM encrypted file unlocked with a scrypt-derived master key on Linux VMs and headless servers; OAuth2 client_secret is stored in the same backend, never in token files), prompt injection is mitigated with a three-layer architecture (structured output schemas + Validator LLM + API-level message boundaries) instead of brittle regex sanitization, sandbox execution is enabled by default (Docker required -- hard error if Docker is missing), document parsing routes through the Docker sandbox, file storage has path traversal protection with magic byte validation, TLS/SSL is always enforced on email connections, the dashboard has CORS restrictions with per-session auth tokens and binds to 127.0.0.1 only, agent names are validated (lowercase alphanumeric + underscores only), error messages are sanitized in API responses, and audit logs record metadata only.

## Architecture Overview

Three interchangeable component types form the core:

- **Agents** -- Thin workflow orchestrators. Compose tools + a provider. Never implement I/O directly.
- **Tools** -- Reusable capabilities (email, document parsing, file storage). Shared across agents via registry.
- **Providers** -- LLM backend abstraction. Any local provider (Ollama, llama.cpp, vLLM, LM Studio, LocalAI, OpenAI-compatible) can be plugged in. Each provider class MUST declare `local_only = True`; the builder rejects anything that doesn't.

All three register via decorators and are discovered at import time by the registry.

### Three-Layer Prompt Injection Defense

Untrusted document text is treated as data, not instructions:

1. **Structured outputs** -- Every LLM call sends a JSON Schema (`response_schema=...`) that constrains the model's response shape. Providers forward this to their native mechanism (Ollama `format`, llama.cpp `json_schema` GBNF, OpenAI-compatible `response_format`). The framework also re-validates the parsed JSON as defense in depth (`secure_agents.core.schemas.validate_schema`).
2. **Validator LLM** -- `secure_agents.core.validator.InputValidator` runs a small screening pass over untrusted text *before* it reaches the primary agent, using the `VALIDATOR_VERDICT_SCHEMA`. It fails closed: any LLM error, schema mismatch, or below-threshold confidence is treated as unsafe.
3. **API-level message boundaries** -- `secure_agents.core.message_builder.MessageBuilder` keeps the system prompt, trusted instructions, and untrusted content in separate `Message` objects. Untrusted content is wrapped in `=== BEGIN UNTRUSTED CONTENT ===` markers and tagged with `name="untrusted_<label>"`. The system prompt never contains user-controlled text.

These three layers replace the old regex-based `sanitize_text()`.

### Config Inheritance

`config.yaml` uses a `defaults` + per-agent override model. `AppConfig.get_agent_config(name)` deep-merges `defaults` with the agent's section. Two agents can have entirely different file size limits, providers, or output directories without touching each other's config.

## Directory Structure

```
src/secure_agents/
  core/
    base_agent.py      # BaseAgent ABC -- DO NOT MODIFY
    base_tool.py       # BaseTool ABC -- DO NOT MODIFY
    base_provider.py   # BaseProvider ABC, Message (with name field), CompletionResponse -- DO NOT MODIFY
    registry.py        # Global Registry singleton, @register_* decorators -- DO NOT MODIFY
    config.py          # AppConfig, ProviderConfig (multi-provider), CredentialsConfig, load_config(), env var interpolation, deep merge
    credential_backends.py # CredentialBackend ABC + KeychainBackend, EncryptedFileBackend (AES-256-GCM + scrypt), EnvBackend, resolve_backend()
    credentials.py     # Thin facade over the active backend (env var fallback always honored). OAuth2 client_secret stored in the backend, not on disk.
    security.py        # File validation (magic byte + extension), filename/path safety, AuditLog (NO regex sanitization)
    schemas.py         # JSON schemas for structured outputs + lightweight schema validator
    validator.py       # InputValidator: secondary LLM that screens untrusted text (fails closed)
    message_builder.py # MessageBuilder: keeps untrusted content in separate, tagged messages
    sandbox.py         # Docker isolated execution (enabled by default, no subprocess fallback)
    job_queue.py       # SQLite-backed job queue (JobQueue, Job, JobStatus), DB file has 0o600 permissions
    metrics.py         # In-memory MetricsCollector singleton
    builder.py         # discover_all(), build_agent() -- wires agents/tools/providers
    logger.py          # structlog setup
  providers/
    ollama.py          # Ollama (uses native `format` for JSON Schema)
    llamacpp.py        # llama.cpp server (/completion with json_schema GBNF)
    openai_compat.py   # OpenAI-compatible local servers: vLLM, LM Studio, LocalAI (response_format json_schema)
  tools/
    email_reader.py    # IMAP inbox monitor, attachment download
    email_sender.py    # SMTP email sending
    document_parser.py # PDF/DOCX text extraction
    file_storage.py    # Local JSON report storage
    _template.py       # Copy this to create a new tool
  agents/
    nda_reviewer/      # Example agent: NDA review via email
      agent.py
      prompts.py
    _template/         # Copy this directory to create a new agent
      __init__.py
      agent.py
  ui/
    server.py          # FastAPI dashboard backend
    dashboard.html     # Single-page web dashboard
  setup/
    manifest.py        # Setup dependency manifest
    steps.py           # Idempotent setup step implementations
    runner.py          # Setup plan executor
  cli.py               # Click CLI: start, list, validate, auth, setup, ui
```

## Agent Interface Contract (BaseAgent)

```python
class BaseAgent(ABC):
    name: str           # Unique identifier, matches config key
    description: str    # Human-readable summary
    features: list[str] # Feature bullet points for dashboard display

    def __init__(self, tools, provider, config=None)
    def setup(self) -> None        # Called once before run loop. Override for init.
    def tick(self) -> None          # ABSTRACT. One iteration of work. Called in a loop.
    def shutdown(self) -> None      # Called once on stop. Override for cleanup.
    def run(self) -> None           # Main loop (do not override). Calls setup/tick/shutdown.
    def request_stop(self) -> None  # Thread-safe stop signal.
    def get_tool(self, name) -> BaseTool
```

Key rules:
- Use `self._stop_event.wait(seconds)` instead of `time.sleep()` for interruptible waits.
- Access merged config via `self.config` (already deep-merged with defaults).
- Access tools via `self.get_tool("tool_name")`, not `self.tools` directly.

## Tool Interface Contract (BaseTool)

```python
class BaseTool(ABC):
    name: str           # Unique identifier, matches registry key
    description: str    # Human-readable summary

    def __init__(self, config=None)
    def execute(self, **kwargs) -> dict   # ABSTRACT. Run the tool's action.
    def validate_config(self) -> bool     # Check required config is present.
```

Tools receive per-agent config, so the same tool class can serve multiple agents with different settings.

## Step-by-Step: Adding a New Agent

1. Copy the template directory:
   ```bash
   cp -r src/secure_agents/agents/_template src/secure_agents/agents/your_agent
   ```

2. Edit `src/secure_agents/agents/your_agent/agent.py`:
   - Uncomment `@register_agent("your_agent")`
   - Set `name`, `description`, `features`
   - Implement `tick()` with your workflow logic

3. Add config to `config.yaml`:
   ```yaml
   agents:
     your_agent:
       enabled: true
       poll_interval_seconds: 60
       tools: [email_reader, document_parser, file_storage]
   ```

4. Run: `secure-agents start your_agent`

The agent is auto-discovered via `registry.discover_plugins()` at startup -- no manual imports needed.

## Step-by-Step: Adding a New Tool

1. Copy the template:
   ```bash
   cp src/secure_agents/tools/_template.py src/secure_agents/tools/your_tool.py
   ```

2. Edit the file:
   - Uncomment `@register_tool("your_tool")`
   - Set `name` and `description`
   - Implement `execute(**kwargs) -> dict`
   - Implement `validate_config()` if the tool needs config

3. Reference it in any agent's config:
   ```yaml
   agents:
     some_agent:
       tools: [your_tool, email_reader]
   ```

4. Use in an agent: `tool = self.get_tool("your_tool")`

See `docs/tool_development.md` for detailed guidance.

## Step-by-Step: Adding a New Trigger Type

Agents are currently triggered by their `tick()` loop (poll-based). To add a new trigger:

1. Implement trigger logic in a new module under `src/secure_agents/core/` (e.g., `webhook_trigger.py`).
2. The trigger should call `job_queue.enqueue(agent_name, payload)` to submit work.
3. The agent's `tick()` can call `job_queue.dequeue(agent_name)` to pick up triggered jobs.
4. Wire the trigger startup into `cli.py` or the `setup()` method of the relevant agent.

## Config Schema

Defined in `src/secure_agents/core/config.py`:

- **AppConfig** (top-level): `defaults`, `provider`, `queue`, `credentials`, `agents`, `max_workers`
- **ProviderConfig**: `active` (str — name of any registered local provider), plus per-provider sub-objects (`ollama`, `llamacpp`, `vllm`, `lmstudio`, `localai`)
- **ProviderSettings**: `host`, `model`, `temperature`
- **QueueConfig**: `db_path`, `max_retries`, `retry_delay_seconds`
- **CredentialsConfig**: `backend` (`auto` | `keychain` | `encrypted_file`), `store_path` (encrypted store location, default `~/.secure-agents/credentials.enc`)

The active provider is selected via `provider.active`. Settings for that provider live under a key matching its name. Any agent can override the provider (or its model/temperature/host) via `agents.<name>.provider.override` etc. The builder verifies the selected provider declares `local_only = True` and raises `ValueError` otherwise.

Environment variable interpolation: `${VAR:default}` syntax in YAML values.

## Job Queue and Workers

`JobQueue` (in `core/job_queue.py`) uses SQLite (`./data/jobs.db`). Supports:
- `enqueue(agent, payload)` -- add a job
- `dequeue(agent)` -- atomically claim the next pending job
- `complete(job_id)` / `fail(job_id, error)` -- update status
- Automatic retry with configurable `max_retries`
- `get_stats()` -- count by status

Agents can use the queue in `tick()` to process work items instead of direct polling.

## Dashboard <-> Backend Communication

The dashboard is a single-page app (`ui/dashboard.html`) served by FastAPI (`ui/server.py`). It binds to 127.0.0.1 only, has CORS restrictions, uses per-session auth tokens, and sanitizes error messages in API responses.

Key API endpoints:
- `GET /api/agents` -- list agents with health, config, run status
- `GET /api/agents/{name}/health` -- detailed health checks
- `POST /api/agents/start` -- start agents in background threads (body: `{"agents": ["name"]}`)
- `POST /api/agents/{name}/stop` -- stop a running agent
- `POST /api/agents/stop-all` -- stop all running agents
- `POST /api/credentials` -- store a credential in Keychain
- `POST /api/test-email` -- test IMAP connection
- `POST /api/config` -- update a single config value by dotted key path
- `GET /api/providers` -- list all registered local providers with availability and `local_only` flag
- `GET /api/tools` -- list registered tools
- `GET /api/metrics` -- agent metrics snapshot

## Common Extension Points

- New agent: `agents/your_agent/agent.py` with `@register_agent`
- New tool: `tools/your_tool.py` with `@register_tool`
- New provider: `providers/your_provider.py` with `@register_provider` — MUST set `local_only = True`, implement `complete(messages, *, model, temperature, json_mode, response_schema)` and `is_available()`. Forward `response_schema` to the backend's native structured-output mechanism.
- New setup steps: `setup/steps.py` (add step functions) + `setup/manifest.py` (declare dependencies)
- New CLI command: `cli.py` (add `@main.command()`)
- New dashboard endpoint: `ui/server.py` (add FastAPI route)

## Files That Should Not Be Modified

These define the framework's contracts. Changing them breaks all agents/tools/providers:

- `src/secure_agents/core/base_agent.py`
- `src/secure_agents/core/base_tool.py`
- `src/secure_agents/core/base_provider.py`
- `src/secure_agents/core/registry.py`

## Naming Conventions and Code Style

- Agent names: lowercase alphanumeric + underscores only (e.g., `nda_reviewer`, `contract_analyzer`) -- validated at registration
- Tool names: `snake_case` (e.g., `email_reader`, `document_parser`)
- Provider names: lowercase (e.g., `ollama`, `llamacpp`, `vllm`, `lmstudio`, `localai`, `openai_compat`)
- Agent directories: `src/secure_agents/agents/<agent_name>/`
- Tool files: `src/secure_agents/tools/<tool_name>.py`
- Provider files: `src/secure_agents/providers/<provider_name>.py`
- Logging: use `structlog.get_logger()`, log metadata only, never content or PII
- Config keys: `snake_case` throughout YAML

## Common Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run tests
pytest tests/

# Validate config and dependencies (checks the active provider, Docker, etc.)
secure-agents validate

# List all registered agents, tools, providers
secure-agents list

# Start a specific agent (requires Docker for sandbox)
secure-agents start nda_reviewer

# Start all enabled agents
secure-agents start

# Initialize an encrypted credential store on a Linux VM / headless server
# (skip on macOS — Keychain is used automatically by the `auto` backend)
secure-agents auth init-store
export SECURE_AGENTS_MASTER_KEY='your-strong-passphrase'

# Show which credential backend is active and what it has stored
secure-agents auth backend

# Store credentials (goes to the active backend — Keychain or encrypted file)
secure-agents auth setup

# Set up Gmail OAuth2 (client_secret stored in the active backend, not on disk)
secure-agents auth gmail path/to/client_secrets.json

# Launch web dashboard (binds to 127.0.0.1 only)
secure-agents ui

# Run the guided setup wizard
secure-agents setup
secure-agents setup nda_reviewer --dry-run

# Verify Docker is available (required -- sandbox is enabled by default)
docker info
```
