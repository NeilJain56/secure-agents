<p align="center">
  <h1 align="center">Secure Agents</h1>
  <p align="center">
    An open-source framework for building AI agents that run entirely on your hardware.<br/>
    Local-only LLM inference. No data leaves your machine. Secure by default.
  </p>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#security">Security</a> &bull;
  <a href="#architecture">Architecture</a> &bull;
  <a href="#building-agents-with-claude-code">Build with Claude Code</a> &bull;
  <a href="#configuration">Configuration</a> &bull;
  <a href="#cli-reference">CLI Reference</a>
</p>

---

## What is Secure Agents?

Secure Agents is a framework for running AI-powered automation workflows **entirely on your own machine**. You define **agents** that orchestrate **tools** (email, document parsing, storage) and a **local LLM** (via [Ollama](https://ollama.com)) — all wired together through config, not code.

**No data ever leaves your machine.** Cloud LLM providers have been deliberately removed from the codebase. Every document, every email, every LLM call stays on your hardware.

It was built for professionals who handle sensitive data — lawyers, financial analysts, compliance officers — but the framework is general-purpose. Any workflow you can describe, you can automate.

**The included NDA Reviewer agent** is a working example: it monitors a Gmail inbox for NDA documents, runs AI-powered clause-by-clause risk analysis using a local LLM, and emails the findings back to the sender.

### Why Secure Agents?

| | Cloud AI Platforms | Secure Agents |
|---|---|---|
| **Data privacy** | Documents sent to third-party servers | **Documents never leave your machine** |
| **LLM inference** | Runs on vendor servers | Runs locally via Ollama |
| **Credentials** | Stored in config files or dashboards | macOS Keychain, never in plaintext |
| **Document parsing** | Server-side, you trust the vendor | Sandboxed in Docker — no network, read-only, ephemeral |
| **Customization** | Vendor lock-in, limited extensibility | Plugin system — add agents/tools with one decorator |
| **Infrastructure** | Cloud accounts, billing, networking | `bash setup.sh` on a Mac. That's it. |

---

## Quick Start

```bash
# Clone and set up (installs Python, Ollama, venv, pulls LLM model)
git clone https://github.com/NeilJain56/secure-agents.git
cd secure-agents
bash setup.sh

# Activate the environment
source .venv/bin/activate

# Store credentials in macOS Keychain (never in files)
secure-agents auth setup

# Edit config with your email settings
cp config.example.yaml config.yaml
nano config.yaml

# Validate everything is wired correctly
secure-agents validate

# Start an agent
secure-agents start nda_reviewer

# Or launch the web dashboard
secure-agents ui
```

### Requirements

- **macOS** (Keychain integration; Linux support planned)
- **Python 3.11+**
- **Ollama** (local LLM inference; installed by `setup.sh`)
- **Docker** (required — sandbox is enabled by default)

---

## Security

This framework was designed from the ground up to be **secure by default**. Every setting ships in its most restrictive state. You must explicitly opt out of security features — not opt in.

### 1. Zero data egress — enforced, not configurable

Cloud LLM providers (Anthropic, OpenAI, Gemini) have been **removed from the codebase**. Not disabled — deleted. There is no config option to send data to an external API. All LLM inference runs locally via Ollama.

### 2. Sandboxed document parsing (enabled by default)

Document parsing (PDF, DOCX) runs inside **Docker containers** with:
- No network access (`--network=none`)
- Read-only filesystem (except `/output`)
- Memory limit (512MB), CPU throttle (50%)
- Automatic destruction after each job

If Docker is missing and sandbox is enabled, the framework **refuses to parse** — it does not silently fall back to native execution. To disable the sandbox, you must explicitly set `security.sandbox_enabled: false` (not recommended).

### 3. Credentials never touch disk in plaintext

Passwords and API keys are **never stored in config files**. The credential resolution chain:

1. **macOS Keychain** — encrypted by the OS, locked to your user account
2. **Environment variables** — for CI/CD or containers
3. **OAuth2 tokens** — access/refresh tokens stored with `0600` permissions; the OAuth2 `client_secret` is stored in the Keychain, never on disk

### 4. Native Gmail OAuth2

No Google App Passwords required. One command sets up OAuth2 with token refresh:

```bash
secure-agents auth gmail path/to/client_secrets.json
```

### 5. File validation with magic bytes

Every file is validated before any parsing library touches it:
- **Magic byte verification** — a `.pdf` must start with `%PDF`, a `.docx` must start with `PK` (ZIP). Renamed executables are rejected.
- File type allowlist (`.pdf`, `.docx` by default)
- Size limits (configurable per agent)
- Path traversal prevention
- Filename sanitization (alphanumeric, dots, hyphens, underscores only; max 255 chars)

### 6. Input sanitization (20+ patterns)

All document text is sanitized before reaching the LLM. The sanitizer:
- Detects **20+ prompt injection patterns** (instruction overrides, role switching, system prompt extraction, data exfiltration attempts)
- Applies **Unicode normalization** (NFKC) to prevent homoglyph-based bypasses
- Logs detection events to the audit trail

### 7. Path traversal protection

File storage operations are **jailed** within the configured output directory. Both filenames and subfolder names are sanitized and resolved, then verified to remain within bounds. Directory traversal (`../`) and absolute paths are rejected.

### 8. Ephemeral processing

Temp files are cleaned up after processing. Sandbox containers are destroyed after each job. The LLM instance cannot retain information between analyses.

### 9. Metadata-only audit logging

The audit log records what happened, when, and to which file — but **never logs document content, email bodies, or PII**. Full operational visibility without creating a second copy of sensitive data.

### 10. Hardened dashboard

The web dashboard:
- Binds to **`127.0.0.1` only** — remote binding is blocked even if requested
- Enforces **strict CORS** — only `localhost` origins allowed
- Requires a **per-session auth token** (generated at startup, printed to terminal, auto-injected into the dashboard) on all state-changing endpoints
- **Sanitizes error messages** — no internal paths, stack traces, or credentials leak to clients

### 11. Minimal attack surface

- SQLite for the job queue (no Redis/RabbitMQ to expose) with `0600` file permissions
- No open ports in agent mode (dashboard is opt-in, localhost only)
- Only local connections to your mail server
- TLS/SSL **always enforced** on email connections — no `use_tls: false` toggle
- Agent names validated (`^[a-z][a-z0-9_]{0,63}$`) to prevent injection via config
- Pinned, minimal dependencies
- No telemetry or analytics

### 12. Supply chain hardening

- Dependencies specified with minimum versions in `pyproject.toml`
- Docker sandbox uses minimal `python:3.12-slim` base with pinned pip packages
- Containers run as a non-root user
- No third-party runtime services required

---

## Architecture

```
                    config.yaml
                        |
                   +---------+
                   | Builder |  (discovers & wires components)
                   +---------+
                   /    |    \
            +-------+ +------+ +----------+
            | Agent | | Tool | | Provider |
            +-------+ +------+ +----------+
               |         |          |
          tick() loop  execute()  complete()
                                     |
                              Ollama (local)
```

### Agents

Workflow orchestrators. Each agent defines *what* to do by composing tools and the LLM provider. Agents are thin — they never implement I/O directly. Adding a new agent is one file and one decorator.

### Tools

Reusable capabilities shared across agents. Declare which tools an agent needs in YAML config:

| Tool | Description |
|------|-------------|
| `email_reader` | Monitor IMAP inbox, download attachments (SSL enforced) |
| `email_sender` | Send emails via SMTP with attachments (TLS enforced) |
| `document_parser` | Extract text from PDF/DOCX (sandboxed via Docker) |
| `file_storage` | Save and load JSON reports locally (path-traversal protected) |

### Provider

Only **Ollama** (local inference) is supported. No data leaves your machine.

| Provider | Type | Default Model |
|----------|------|---------------|
| `ollama` | Local | `llama3.2` |

### Plugin System

All components register via decorators and are auto-discovered at startup:

```python
@register_agent("my_agent")     # Agents
@register_tool("my_tool")       # Tools
```

No manual imports or wiring. Drop a file in the right directory, add a decorator, and it's available.

---

## Building Agents with Claude Code

The fastest way to add a new agent is with [Claude Code](https://docs.anthropic.com/en/docs/claude-code). You don't write framework boilerplate — you describe the workflow you want, and Claude Code builds it using the framework's patterns.

### The Workflow

**Step 1.** Open Claude Code in the project directory:

```bash
cd secure-agents
claude
```

**Step 2.** Give Claude Code the context file, then describe your agent:

```
@.claude/agent-development.md

Build me an agent called "contract_analyzer" that:
- Monitors my inbox for contract documents (PDF/DOCX)
- Extracts text and identifies key clauses (termination, liability, IP ownership)
- Uses the LLM to score risk on each clause
- Saves a structured JSON report
- Emails a summary back to the sender

Use the existing email_reader, document_parser, file_storage, and email_sender tools.
Follow the same patterns as the nda_reviewer agent.
```

**Step 3.** Claude Code will:
- Create `src/secure_agents/agents/contract_analyzer/agent.py` with proper registration
- Create `src/secure_agents/agents/contract_analyzer/prompts.py` with domain-specific prompts
- Add the config entry to `config.yaml`
- Write tests in `tests/test_contract_analyzer.py`
- Wire everything to the existing framework

**Step 4.** Run it:

```bash
secure-agents start contract_analyzer
```

### Example Prompts for Common Agent Types

**Email triage agent:**
```
@.claude/agent-development.md

Build an agent called "email_triage" that reads incoming emails, classifies them
into categories (urgent, follow-up, informational, spam) using the LLM, and saves
a daily digest report. Don't send any emails back — just categorize and store.
```

**Compliance monitor:**
```
@.claude/agent-development.md

Build an agent called "compliance_checker" that watches a folder for new policy
documents, compares them against a set of regulatory requirements I'll define in
the prompts file, and flags any gaps or violations. Save reports with risk scores.
```

**Research assistant:**
```
@.claude/agent-development.md

Build an agent called "research_digest" that monitors email for newsletters and
research papers, extracts the key findings using the LLM, and saves a structured
summary. Group by topic. I want to review the summaries on the dashboard.
```

### Tips for Best Results

- **Always reference the context file** with `@.claude/agent-development.md` — it gives Claude Code the full framework contracts, naming conventions, and security rules.
- **Reference the NDA reviewer** as a pattern to follow: `Look at src/secure_agents/agents/nda_reviewer/agent.py for the pattern.`
- **Be specific about tools** — name which existing tools to use (`email_reader`, `document_parser`, etc.) so Claude Code wires them correctly.
- **Ask for tests** — Claude Code will write pytest tests using mocks, matching the project's testing patterns.
- **Ask for prompts in a separate file** — keep LLM system prompts in a `prompts.py` file, not inline in the agent.

### Creating New Tools with Claude Code

```
@.claude/agent-development.md

Build a new tool called "slack_notifier" that sends messages to a Slack webhook.
It should accept channel, message, and optional severity level. Store the webhook
URL in the credential system (macOS Keychain), never in config. Follow the BaseTool
interface and update builder.py to wire the config.
```

### The Context File

The file `.claude/agent-development.md` contains everything Claude Code needs:
- Framework contracts (BaseAgent, BaseTool, BaseProvider interfaces)
- Existing tools and provider with their parameters
- Config inheritance model
- All 13 security rules
- Testing patterns
- The NDA Reviewer as a reference implementation

This file is checked into the repo. Keep it updated as you add new tools or change contracts.

---

## Configuration

`config.yaml` uses a **defaults + per-agent override** model:

```yaml
# Only Ollama (local) is supported — no cloud providers
provider:
  active: ollama
  ollama:
    host: http://localhost:11434
    model: llama3.2

# Shared defaults — every agent inherits these
defaults:
  email:
    imap:
      host: imap.gmail.com
      username: you@gmail.com
      auth_method: oauth2
  security:
    max_file_size_mb: 50
    allowed_file_types: [.pdf, .docx]
    sandbox_enabled: true     # Requires Docker (default: on)

# Each agent inherits defaults and overrides what it needs
agents:
  nda_reviewer:
    enabled: true
    poll_interval_seconds: 60
    tools: [email_reader, email_sender, document_parser, file_storage]

  contract_analyzer:
    enabled: true
    tools: [email_reader, document_parser, file_storage]
    security:
      max_file_size_mb: 200          # Larger files for this agent
```

Two agents can have entirely different file size limits, output directories, or polling intervals without affecting each other. See `config.example.yaml` for the full annotated template.

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `secure-agents start [agents...]` | Start one, several, or all enabled agents |
| `secure-agents list` | List registered agents, tools, and provider |
| `secure-agents validate` | Check config, Ollama, Docker, credentials |
| `secure-agents auth setup` | Store credentials in macOS Keychain |
| `secure-agents auth gmail <secrets.json>` | Set up Gmail OAuth2 |
| `secure-agents setup [agents...]` | Guided setup wizard |
| `secure-agents ui` | Launch the web dashboard (localhost only) |

---

## Web Dashboard

A single-page dashboard (no npm, no build step) for monitoring and controlling agents:

- **Agents tab** — Start/stop agents, view health status, enable/disable toggles
- **Metrics tab** — Job counts, error rates, processing latency, time-series charts
- **Outputs tab** — Browse and view generated reports
- **Audit Log tab** — Searchable metadata-only event log

Launch with `secure-agents ui` and open `http://localhost:8420`. The dashboard binds to localhost only, uses strict CORS, and requires a per-session auth token on all state-changing operations.

---

## Project Structure

```
src/secure_agents/
  core/             # Framework internals
    base_agent.py       BaseAgent ABC (the agent contract)
    base_tool.py        BaseTool ABC (the tool contract)
    base_provider.py    BaseProvider ABC (the provider contract)
    registry.py         Plugin registry and @register_* decorators
    config.py           Config loading, validation, env var interpolation
    credentials.py      Keychain / env var / OAuth2 credential resolution
    security.py         File validation (magic bytes), input sanitization, audit log
    sandbox.py          Docker-only sandboxed execution (no fallback)
    job_queue.py        SQLite-backed job queue with retry and dead-letter
    builder.py          Discovers all plugins and wires agents at startup

  providers/        # LLM backend (local only)
    ollama.py           Local inference via Ollama

  tools/            # Reusable capabilities (shared across agents)
    email_reader.py     IMAP inbox monitor (SSL enforced)
    email_sender.py     SMTP email sending (TLS enforced)
    document_parser.py  PDF/DOCX text extraction (sandboxed)
    file_storage.py     Local JSON report storage (path-traversal protected)
    _template.py        Copy this to create a new tool

  agents/           # Agent implementations
    nda_reviewer/       Example: NDA review via email monitoring
    _template/          Copy this directory to create a new agent

  ui/               # Web dashboard
    server.py           FastAPI backend (localhost only, auth token, CORS)
    dashboard.html      Single-page frontend (no build step)
```

---

## Development

```bash
# Install in development mode
pip install -e ".[dev]"

# Run tests (59 tests including security suite)
pytest tests/ -v

# Validate config, Ollama, Docker
secure-agents validate

# List all registered components
secure-agents list
```

---

## License

MIT
