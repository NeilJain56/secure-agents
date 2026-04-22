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

Secure Agents is a framework for running AI-powered automation workflows **entirely on your own machine**. You define **agents** that orchestrate **tools** (email, document parsing, storage) and a **local LLM** — all wired together through config, not code. The provider layer is pluggable: use [Ollama](https://ollama.com), [llama.cpp](https://github.com/ggerganov/llama.cpp), [vLLM](https://github.com/vllm-project/vllm), [LM Studio](https://lmstudio.ai/), [LocalAI](https://localai.io/), or any OpenAI-compatible local server.

**No data ever leaves your machine.** Every provider must declare `local_only = True`; the builder rejects anything that doesn't. Every document, every email, every LLM call stays on your hardware.

It was built for professionals who handle sensitive data — lawyers, financial analysts, compliance officers — but the framework is general-purpose. Any workflow you can describe, you can automate.

**The included NDA Reviewer agent** is a working example: it monitors a Gmail inbox for NDA documents, runs AI-powered clause-by-clause risk analysis using a local LLM, and emails the findings back to the sender.

### Why Secure Agents?

| | Cloud AI Platforms | Secure Agents |
|---|---|---|
| **Data privacy** | Documents sent to third-party servers | **Documents never leave your machine** |
| **LLM inference** | Runs on vendor servers | Runs locally via your choice of backend (Ollama, llama.cpp, vLLM, LM Studio, LocalAI) |
| **Credentials** | Stored in config files or dashboards | macOS Keychain *or* AES-256-GCM encrypted file (Linux/VM), never in plaintext |
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

# Store credentials in the active backend (Keychain on macOS, encrypted file elsewhere)
# On a Linux VM, run `secure-agents auth init-store` once first.
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

- **macOS or Linux** — macOS uses the Keychain automatically; Linux/headless servers use the encrypted file backend (initialize once with `secure-agents auth init-store`)
- **Python 3.11+**
- A local LLM backend — Ollama (installed by `setup.sh`), or any of: llama.cpp server, vLLM, LM Studio, LocalAI
- **Docker** (required — sandbox is enabled by default)

---

## Security

This framework was designed from the ground up to be **secure by default**. Every setting ships in its most restrictive state. You must explicitly opt out of security features — not opt in.

### 1. Zero data egress — enforced, not configurable

Cloud LLM providers (Anthropic, OpenAI, Gemini) have been **removed from the codebase**. Not disabled — deleted. There is no config option to send data to an external API. Every provider class must declare `local_only = True` and the builder raises `ValueError` at startup if it doesn't. The `openai_compat` provider additionally rejects non-local hostnames at config time. You can plug in any local backend — Ollama, llama.cpp, vLLM, LM Studio, LocalAI — but cloud egress is structurally impossible.

### 2. Sandboxed document parsing (enabled by default)

Document parsing (PDF, DOCX) runs inside **Docker containers** with:
- No network access (`--network=none`)
- Read-only filesystem (except `/output`)
- Memory limit (512MB), CPU throttle (50%)
- Automatic destruction after each job

If Docker is missing and sandbox is enabled, the framework **refuses to parse** — it does not silently fall back to native execution. To disable the sandbox, you must explicitly set `security.sandbox_enabled: false` (not recommended).

### 3. Credentials never touch disk in plaintext

Passwords and API keys are **never stored in config files**. The credential layer is a pluggable backend chosen via `credentials.backend` in `config.yaml`:

| Backend | Where it stores secrets | When to use |
|---------|------------------------|-------------|
| `auto` *(default)* | macOS Keychain when available, otherwise the encrypted file backend | Sensible default everywhere |
| `keychain` | macOS Keychain — encrypted by the OS, locked to your user account | macOS laptops/workstations |
| `encrypted_file` | AES-256-GCM encrypted JSON store at `~/.secure-agents/credentials.enc` (mode `0600`), key derived from a master passphrase via scrypt | **Linux VMs and headless servers** — strictly more secure than env vars |

The encrypted file backend:
- Uses **AES-256-GCM** authenticated encryption (any tampering is detected)
- Derives the data key with **scrypt** (`N=2^15, r=8, p=1`) — slow to brute-force
- Reads the master passphrase from `SECURE_AGENTS_MASTER_KEY` or an interactive `getpass` prompt — never from a file
- **Refuses to load** if the store has world- or group-readable permissions
- **Fails closed** on a wrong passphrase: cached key is dropped, no plaintext is leaked
- Uses **atomic writes** (`tempfile` + `os.replace`) so the store can never be corrupted mid-write

In addition to the configured backend, **environment variables are always consulted as a per-secret fallback**, so `EMAIL_PASSWORD=... secure-agents start ...` still works for one-off overrides without re-running setup.

OAuth2 tokens (access + refresh) are stored under `~/.secure-agents/tokens/` with `0600` permissions; the OAuth2 `client_secret` lives in the configured credential backend, **never** on disk.

To bring up an encrypted store on a Linux VM:

```bash
secure-agents auth init-store                    # prompts for master passphrase
export SECURE_AGENTS_MASTER_KEY='strong-passphrase'   # or set it in your systemd unit
secure-agents auth setup                          # store individual credentials
secure-agents auth backend                        # confirm what's active and stored
```

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

### 6. Three-layer prompt injection defense

Regex-based input scrubbing was deleted in favor of a structural defense that does not depend on enumerating attack patterns:

1. **Structured outputs** — Every LLM call sends a JSON Schema (`response_schema=...`) that constrains the model to a specific shape. Each provider forwards this to its native mechanism (Ollama's `format`, llama.cpp's `json_schema` GBNF, OpenAI-compatible `response_format`). Returned JSON is also re-validated as defense in depth.
2. **Validator LLM** — A second, smaller model screens untrusted text *before* it reaches the primary agent and returns a `{verdict, confidence, reasons}` object against `VALIDATOR_VERDICT_SCHEMA`. It fails closed: any LLM error, schema mismatch, or below-threshold confidence is treated as unsafe and the document is rejected.
3. **API-level message boundaries** — Untrusted document text is placed in a separate `Message` with `role="user"`, `name="untrusted_<label>"`, and `=== BEGIN UNTRUSTED CONTENT ===` markers. The system prompt never contains user-controlled text, so embedded instructions cannot rewrite the agent's directives.

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
                   | Builder |  (discovers & wires components, enforces local_only)
                   +---------+
                  / |    |    \
          +-------+ +------+ +----------+ +----------+
          | Agent | | Tool | | Provider | | JobQueue |
          +-------+ +------+ +----------+ +----------+
             |         |          |             |
        tick() loop  execute()  complete()  emit() → enqueue()
             |                    |
        self.emit()    any local backend with local_only=True:
        (hand off)     ollama | llamacpp | vllm | lmstudio | localai
```

### Agents

Workflow orchestrators. Each agent defines *what* to do by composing tools and the LLM provider. Agents are thin — they never implement I/O directly. Adding a new agent is one file and one decorator.

Agents can **hand off work to other agents** via a shared SQLite-backed job queue. Call `self.emit("other_agent", {...})` in your `tick()` method — the payload appears as a pending job for the target agent. Common patterns:

- **Sequential handoff:** Agent A reviews a document, then emits to Agent B for notification.
- **Parallel fan-out:** Agent A emits to both Agent B (summarize) and Agent C (archive) in one tick.
- **Orchestrator:** A central agent routes work to different downstream agents based on a state flag.

### Tools

Reusable capabilities shared across agents. Declare which tools an agent needs in YAML config:

| Tool | Description |
|------|-------------|
| `email_reader` | Monitor IMAP inbox, download attachments (SSL enforced) |
| `email_sender` | Send emails via SMTP with attachments (TLS enforced) |
| `document_parser` | Extract text from PDF/DOCX (sandboxed via Docker) |
| `file_storage` | Save and load JSON reports locally (path-traversal protected) |
| `text_extractor` | Extract text from PDF/DOCX/DOC/PPTX/XLSX (trusted local files, no sandbox) |
| `file_manager` | Scan directories, copy files, create folders, write CSVs (path-jailed) |

### Providers

Pluggable local backends. Pick whichever runs best on your hardware. Every provider declares `local_only = True`.

| Provider | Backend | Notes |
|----------|---------|-------|
| `ollama` | [Ollama](https://ollama.com) | Native `format` JSON Schema support |
| `llamacpp` | [llama.cpp server](https://github.com/ggerganov/llama.cpp) | `json_schema` GBNF grammar at `/completion` |
| `vllm` | [vLLM](https://github.com/vllm-project/vllm) | OpenAI-compatible `response_format` |
| `lmstudio` | [LM Studio](https://lmstudio.ai/) | OpenAI-compatible `response_format` |
| `localai` | [LocalAI](https://localai.io/) | OpenAI-compatible `response_format` |
| `openai_compat` | Any local OpenAI-compatible server | Hostname must resolve to a private/loopback address |

Adding another local backend is one file: implement `complete(messages, *, response_schema, ...)` and `is_available()`, set `local_only = True`, decorate with `@register_provider("name")`.

### Plugin System

All components register via decorators and are auto-discovered at startup:

```python
@register_agent("my_agent")     # Agents
@register_tool("my_tool")       # Tools
```

No manual imports or wiring. Drop a file in the right directory, add a decorator, and it's available.

---

## Document Sorting & Dedup Pipeline

A built-in multi-agent pipeline that sorts a folder of mixed legal documents into three categories and finds near-duplicates within each category — tested on a real corpus of 154 legal documents.

### Pipeline flow

```
source_folder/  (PDF, DOCX, DOC, PPTX, XLSX)
      │
      ▼  Stage 1 — serial
 ┌────────────┐  adaptive chunking: 1,500 → 3,500 → 6,000 chars
 │ doc_sorter │  one LLM call per file, 4 parallel workers
 └─────┬──────┘  copies files into category subfolders
       │ emit() × 3
       ▼  Stage 2 — all three run in parallel
 ┌───────────────┐  ┌──────────────────────┐  ┌───────────────────────────┐
 │ nda_dedup     │  │ msa_company_dedup    │  │ msa_thirdparty_dedup      │
 │ Jaccard ≥0.95 │  │ Jaccard ≥0.95        │  │ Jaccard ≥0.95             │
 │ → LLM compare │  │ → LLM compare        │  │ → LLM compare             │
 └───────┬───────┘  └──────────┬───────────┘  └─────────────┬─────────────┘
         ▼                     ▼                             ▼
 NDAs/                   MSAs (company)/                MSAs (third party)/
   duplicates.csv          duplicates.csv                 duplicates.csv
```

### Quick start

1. Set `source_folder` and `output_root` in `config.yaml` to point at your folder.
2. Run the pipeline with one command:
   ```bash
   secure-agents start doc_sort_pipeline
   ```
3. Results appear in `output_root` with one `duplicates.csv` per category.

The CSV format: `file_a, file_b, confidence, reasoning`.

### How dedup works

1. Extract full text from every file in a category folder.
2. **Jaccard pre-filter** (stdlib, instant) — compute word-set similarity for every pair; skip any pair below 0.95. Eliminates ~95% of pairs before any LLM call.
3. **LLM comparison** on candidate pairs — send the first 4,000 chars of each document; model returns `is_similar`, `confidence`, and `reasoning` as structured JSON.

For the MSA-thirdparty category in our test set (~60–70 files, ~109 candidate pairs after pre-filtering), Stage 2 runs in ~16 minutes. All three categories run in parallel, so total dedup time equals the slowest category.

→ **[Full pipeline documentation](docs/doc_sort_pipeline.md)** — architecture deep dive, performance numbers, configuration reference, and output format.

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
# Pick any local provider — every backend must declare local_only=True
provider:
  active: ollama         # or: llamacpp, vllm, lmstudio, localai
  ollama:
    host: http://localhost:11434
    model: llama3.2
  llamacpp:
    host: http://localhost:8080
    model: default
  vllm:
    host: http://localhost:8000
    model: meta-llama/Llama-3.2-3B-Instruct

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
| `secure-agents list` | List registered agents, tools, and providers |
| `secure-agents validate` | Check config, the active provider, Docker, credentials |
| `secure-agents auth init-store` | Create an encrypted credential store (Linux VMs / headless) |
| `secure-agents auth backend` | Show the active credential backend and what it has stored |
| `secure-agents auth setup` | Store credentials in the active backend |
| `secure-agents auth gmail <secrets.json>` | Set up Gmail OAuth2 (client_secret stored in the backend, not on disk) |
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
    credential_backends.py  Pluggable backends: Keychain, EncryptedFile (AES-GCM + scrypt), Env
    credentials.py      Thin facade over the active backend + OAuth2 helpers
    job_queue.py        SQLite-backed job queue for multi-agent sequencing
    security.py         File validation (magic bytes), filename/path safety, audit log
    schemas.py          JSON schemas + lightweight validator for structured outputs
    validator.py        InputValidator: secondary LLM that fails closed on unsafe input
    message_builder.py  Keeps untrusted text in tagged, separate Messages
    sandbox.py          Docker-only sandboxed execution (no fallback)
    job_queue.py        SQLite-backed job queue with retry and dead-letter
    builder.py          Discovers all plugins, wires agents, enforces local_only

  providers/        # LLM backends (every one declares local_only=True)
    ollama.py           Ollama (native format JSON Schema)
    llamacpp.py         llama.cpp server (json_schema GBNF)
    openai_compat.py    vLLM / LM Studio / LocalAI / generic openai_compat

  tools/            # Reusable capabilities (shared across agents)
    email_reader.py     IMAP inbox monitor (SSL enforced)
    email_sender.py     SMTP email sending (TLS enforced)
    document_parser.py  PDF/DOCX text extraction (sandboxed)
    file_storage.py     Local JSON report storage (path-traversal protected)
    text_extractor.py   PDF/DOCX/DOC/PPTX/XLSX text extraction (trusted local files)
    file_manager.py     Dir scan, copy, mkdir, CSV write (path-jailed)
    _template.py        Copy this to create a new tool

  agents/           # Agent implementations
    nda_reviewer/       Email-triggered NDA review agent
    doc_sorter/         Document classification pipeline (→ 3 category folders)
    deduplicator/       Near-duplicate detection (1 base, 3 registrations)
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

# Run tests
pytest tests/ -v

# Validate config, active provider, Docker
secure-agents validate

# List all registered components
secure-agents list
```

---

## License

MIT
