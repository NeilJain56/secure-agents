<p align="center">
  <h1 align="center">Secure Agents</h1>
  <p align="center">
    An open-source framework for building AI agents that run entirely on your hardware.<br/>
    Modular. Secure by default. No data leaves your machine.
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

Secure Agents is a framework for running AI-powered automation workflows on your own machine. You define **agents** that orchestrate **tools** (email, document parsing, storage) and **LLM providers** (Ollama, Anthropic, OpenAI, Gemini) — all wired together through config, not code.

It was built for professionals who handle sensitive data — lawyers, financial analysts, compliance officers — but the framework is general-purpose. Any workflow you can describe, you can automate.

**The included NDA Reviewer agent** is a working example: it monitors a Gmail inbox for NDA documents, runs AI-powered clause-by-clause risk analysis using a local LLM, and emails the findings back to the sender. No document ever leaves your laptop.

### Why Secure Agents?

| | Cloud AI Platforms | Secure Agents |
|---|---|---|
| **Data privacy** | Documents sent to third-party servers | Documents never leave your machine |
| **Credentials** | Stored in config files or dashboards | macOS Keychain, never in plaintext |
| **Customization** | Vendor lock-in, limited extensibility | Plugin system — add agents/tools/providers with one decorator |
| **Infrastructure** | Requires cloud accounts, billing, networking | `bash setup.sh` on a Mac. That's it. |
| **LLM flexibility** | Single provider | Swap between Ollama (local), Anthropic, OpenAI, Gemini via config |

---

## Quick Start

```bash
# Clone and set up (installs Python, Ollama, venv, pulls LLM model)
git clone https://github.com/yourorg/secure-agents.git
cd secure-agents
bash setup.sh

# Activate the environment
source .venv/bin/activate

# Store credentials in macOS Keychain (never in files)
secure-agents auth setup

# Edit config with your email, provider choice, etc.
cp config.example.yaml config.yaml
nano config.yaml

# Validate everything is wired correctly
secure-agents validate

# Start an agent
secure-agents start nda_reviewer

# Or launch the web dashboard
secure-agents ui
```

---

## Security

This framework was designed from the ground up for sensitive data. Every layer assumes the data is confidential.

### 1. Zero data exfiltration by default

With Ollama (the default provider), **no data ever leaves your machine**. The LLM runs as a local process. Cloud providers (Anthropic, OpenAI, Gemini) are explicit opt-in via config — the framework never silently sends data anywhere.

### 2. Credentials never touch disk in plaintext

Passwords and API keys are **never stored in config files**. The credential resolution chain:

1. **macOS Keychain** — encrypted by the OS, locked to your user account
2. **Environment variables** — for CI/CD or containers
3. **OAuth2 tokens** — stored with `0600` permissions, auto-refreshed

`config.yaml` contains only non-secret settings. Safe to commit to version control.

### 3. Native Gmail OAuth2

No Google App Passwords required. One command sets up OAuth2 with token refresh:

```bash
secure-agents auth gmail path/to/client_secrets.json
```

### 4. Sandboxed execution

Document parsing and LLM inference can run inside Docker containers:
- No network access (`--network=none`)
- Read-only filesystem (except `/output`)
- Memory and CPU limits
- Auto-destroyed after each job

Falls back to subprocess isolation if Docker is unavailable.

### 5. Input sanitization

All document text is sanitized before reaching the LLM. Known prompt injection patterns are filtered: instruction overrides, role-switching, system prompt extraction attempts. Defense-in-depth — the system prompt hierarchy is the primary defense; sanitization is a secondary filter.

### 6. File validation

Every file is validated before parsing:
- File type allowlist (`.pdf`, `.docx` by default)
- Size limits (configurable per agent)
- Path traversal prevention
- Filename sanitization

### 7. Ephemeral processing

Temp files are cleaned up after processing. Sandbox environments are destroyed after each job. When using Docker, the LLM instance cannot retain information between analyses.

### 8. Metadata-only audit logging

The audit log records what happened, when, and to which file — but **never logs document content, email bodies, or PII**. Full operational visibility without creating a second copy of sensitive data.

### 9. Minimal attack surface

- SQLite for the job queue (no Redis/RabbitMQ to expose)
- No open ports in agent mode (dashboard is opt-in)
- Only local connections to your mail server
- Pinned, minimal dependencies
- No telemetry or analytics

### 10. Supply chain hardening

- Dependencies specified with minimum versions in `pyproject.toml`
- Docker sandbox uses minimal `python:3.12-slim` base
- Containers run as non-root
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
```

### Agents

Workflow orchestrators. Each agent defines *what* to do by composing tools and an LLM provider. Agents are thin — they never implement I/O directly. Adding a new agent is one file and one decorator.

### Tools

Reusable capabilities shared across agents. Declare which tools an agent needs in YAML config:

| Tool | Description |
|------|-------------|
| `email_reader` | Monitor IMAP inbox, download attachments |
| `email_sender` | Send emails via SMTP with attachments |
| `document_parser` | Extract text from PDF and DOCX securely |
| `file_storage` | Save and load JSON reports locally |

### Providers

LLM backends with a unified interface. Switch between local and cloud with one config line:

| Provider | Type | Default Model |
|----------|------|---------------|
| `ollama` | Local | `llama3.2` |
| `anthropic` | Cloud | `claude-sonnet-4-20250514` |
| `openai` | Cloud | `gpt-4o` |
| `gemini` | Cloud | `gemini-2.5-flash` |

### Plugin System

All components register via decorators and are auto-discovered at startup:

```python
@register_agent("my_agent")     # Agents
@register_tool("my_tool")       # Tools
@register_provider("my_llm")    # Providers
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

You can also build new tools:

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
- Existing tools and providers with their parameters
- Config inheritance model
- Security rules and naming conventions
- Testing patterns
- The NDA Reviewer as a reference implementation

This file is checked into the repo. Keep it updated as you add new tools or change contracts.

---

## Configuration

`config.yaml` uses a **defaults + per-agent override** model:

```yaml
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
    provider:
      override: anthropic            # Use Claude instead of Ollama
```

Two agents can have entirely different file size limits, output directories, or LLM providers without affecting each other. See `config.example.yaml` for the full annotated template.

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `secure-agents start [agents...]` | Start one, several, or all enabled agents |
| `secure-agents list` | List registered agents, tools, and providers |
| `secure-agents validate` | Check config, credentials, provider connectivity |
| `secure-agents auth setup` | Store credentials in macOS Keychain |
| `secure-agents auth gmail <secrets.json>` | Set up Gmail OAuth2 |
| `secure-agents setup [agents...]` | Guided setup wizard |
| `secure-agents ui` | Launch the web dashboard |

---

## Web Dashboard

A single-page dashboard (no npm, no build step) for monitoring and controlling agents:

- **Agents tab** — Start/stop agents, view health status, enable/disable toggles
- **Metrics tab** — Job counts, error rates, processing latency, time-series charts
- **Outputs tab** — Browse and view generated reports
- **Audit Log tab** — Searchable metadata-only event log

Launch with `secure-agents ui` and open `http://localhost:8000`.

---

## Project Structure

```
src/secure_agents/
  core/             # Framework internals
    base_agent.py       BaseAgent ABC (the agent contract)
    base_tool.py        BaseTool ABC (the tool contract)
    base_provider.py    BaseProvider ABC (the provider contract)
    registry.py         Plugin registry and @register_* decorators
    config.py           Config loading, deep merge, env var interpolation
    credentials.py      Keychain / env var / OAuth2 credential resolution
    security.py         File validation, input sanitization, audit log
    sandbox.py          Docker and subprocess sandboxed execution
    job_queue.py        SQLite-backed job queue with retry and dead-letter
    builder.py          Discovers all plugins and wires agents at startup

  providers/        # LLM backends (all implement the same interface)
    ollama.py           Local inference via Ollama
    anthropic_provider.py
    openai_provider.py
    gemini_provider.py

  tools/            # Reusable capabilities (shared across agents)
    email_reader.py     IMAP inbox monitor with attachment download
    email_sender.py     SMTP email sending
    document_parser.py  PDF/DOCX text extraction
    file_storage.py     Local JSON report storage
    _template.py        Copy this to create a new tool

  agents/           # Agent implementations
    nda_reviewer/       Example: NDA review via email monitoring
    _template/          Copy this directory to create a new agent

  ui/               # Web dashboard
    server.py           FastAPI backend
    dashboard.html      Single-page frontend (no build step)
```

---

## Development

```bash
# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Validate config and dependencies
secure-agents validate

# List all registered components
secure-agents list
```

---

## Requirements

- **macOS** (Keychain integration; Linux support planned)
- **Python 3.11+**
- **Ollama** (for local LLM inference; installed by `setup.sh`)
- **Docker** (optional, for sandboxed execution)

---

## License

MIT
