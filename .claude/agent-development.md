# Agent Development Context for Claude Code

This file provides context for Claude Code when building new agents, tools, or providers for the Secure Agents framework. Reference this file in your Claude Code prompt with: `/read .claude/agent-development.md`

---

## Framework Architecture (What You Need to Know)

Secure Agents has three component types, all registered via decorators:

- **Agent** (`@register_agent`) — Orchestrates a workflow by composing tools + an LLM provider. Lives in `src/secure_agents/agents/<name>/agent.py`.
- **Tool** (`@register_tool`) — A reusable capability (email, parsing, storage, API calls). Lives in `src/secure_agents/tools/<name>.py`.
- **Provider** (`@register_provider`) — LLM backend abstraction. Any local backend can be plugged in (`ollama`, `llamacpp`, `vllm`, `lmstudio`, `localai`, `openai_compat`). Each provider class MUST set `local_only = True`; the builder rejects anything else. Lives in `src/secure_agents/providers/<name>.py`.

All three are auto-discovered at import time. No manual registration required.

---

## Contracts (Do NOT Change These)

### BaseAgent (src/secure_agents/core/base_agent.py)

```python
class BaseAgent(ABC):
    name: str               # Must match the config key and @register_agent name
    description: str        # Shown in CLI and dashboard
    version: str            # Semver string
    features: list[str]     # Bullet points for the dashboard

    def __init__(self, tools: dict, provider: BaseProvider, config: dict | None = None, *, job_queue: JobQueue | None = None)
    def setup(self) -> None          # Optional hook, called once before the loop
    def tick(self) -> None           # REQUIRED. One iteration of work.
    def shutdown(self) -> None       # Optional hook, called once on stop
    def run(self) -> None            # Framework loop — do NOT override
    def request_stop(self) -> None   # Thread-safe stop signal
    def get_tool(self, name) -> BaseTool  # Lookup a tool by registered name
    def emit(self, agent: str, payload: dict) -> None  # Enqueue a job for another agent (no-op if no queue)
```

Rules:
- `tick()` is called in a loop. Do your work, then call `self._stop_event.wait(seconds)` to sleep.
- NEVER use `time.sleep()` — it blocks clean shutdown.
- Access tools via `self.get_tool("name")`, not `self.tools` directly.
- `self.config` is already deep-merged (defaults + agent overrides).
- `self.provider.complete(messages, response_schema=...)` calls the LLM. Messages use `Message(role=, content=, name=)`. ALWAYS pass a `response_schema` for structured outputs and build messages with `MessageBuilder`, not by hand.
- To hand off work to another agent, call `self.emit("target_agent", {"key": "value"})`. NEVER call `self.job_queue.enqueue()` directly. `emit()` is a safe no-op when no queue is wired.
- The `job_queue` parameter is optional (default `None`). The builder passes a shared `JobQueue` instance automatically — you never need to create one yourself.

### BaseTool (src/secure_agents/core/base_tool.py)

```python
class BaseTool(ABC):
    name: str
    description: str

    def __init__(self, config: dict | None = None)
    def execute(self, **kwargs) -> dict      # REQUIRED. Returns results dict.
    def validate_config(self) -> bool        # Health check. Keep fast.
```

Rules:
- Always return a dict from `execute()`.
- On failure, include an `"error"` key in the return dict.
- Use `get_credential("key")` from `core/credentials.py` for secrets — NEVER config files.
- Each agent gets its own tool instance with its own config.

### BaseProvider (src/secure_agents/core/base_provider.py)

```python
@dataclass
class Message:
    role: str          # "system" | "user" | "assistant"
    content: str
    name: str = ""     # optional provenance tag (e.g. "untrusted_document")

class BaseProvider(ABC):
    local_only: bool = True   # MUST stay True for any provider in this framework

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        response_schema: dict | None = None,
    ) -> CompletionResponse: ...

    def is_available(self) -> bool: ...
```

When `response_schema` is provided, the provider MUST forward it to its backend's native structured-output mechanism (Ollama `format`, llama.cpp `json_schema`, OpenAI-compatible `response_format` with `json_schema`).

---

## Existing Tools (Reuse These)

| Tool Name | What It Does | Key kwargs |
|-----------|-------------|------------|
| `email_reader` | IMAP inbox monitor | `folder`, `mark_read`, `since_days`, `max_emails` |
| `email_sender` | SMTP email sending | `to`, `subject`, `body`, `attachments` |
| `document_parser` | PDF/DOCX text extraction | `file_path` |
| `file_storage` | JSON report save/load | `action` ("save"/"load"/"list"), `filename`, `data`, `subfolder` |

---

## Providers

Pluggable local LLM backends. Cloud providers have been removed entirely; every provider class declares `local_only = True` and the builder verifies it. No data ever leaves the machine.

| Provider Name | Backend | Config Key | How structured outputs work |
|---------------|---------|------------|-----------------------------|
| `ollama` | Ollama | `ollama` | Native `format: <schema>` |
| `llamacpp` | llama.cpp server | `llamacpp` | `json_schema` GBNF grammar at `/completion` |
| `vllm` | vLLM | `vllm` | OpenAI-compatible `response_format` |
| `lmstudio` | LM Studio | `lmstudio` | OpenAI-compatible `response_format` |
| `localai` | LocalAI | `localai` | OpenAI-compatible `response_format` |
| `openai_compat` | Any local OpenAI-compatible server | (uses defaults) | Hostname must resolve to private/loopback |

The active provider is selected via `provider.active`. Per-agent overrides go under `agents.<name>.provider.override` (with optional `model`, `temperature`, `host`).

---

## Config Inheritance Model

`config.yaml` has `defaults:` (shared) and per-agent sections under `agents:`. When building an agent, the framework deep-merges `defaults` with the agent's section. Agents only override what they need.

```yaml
defaults:
  email:
    imap: { host: imap.gmail.com, port: 993 }
  security:
    max_file_size_mb: 50
    sandbox_enabled: true       # Default, requires Docker

provider:
  active: ollama                # any registered local provider
  ollama:
    host: http://localhost:11434
    model: llama3.2
  llamacpp:
    host: http://localhost:8080
    model: default

agents:
  my_agent:
    enabled: true
    tools: [email_reader, document_parser]
    security:
      max_file_size_mb: 200    # Override just this one value
```

---

## How to Wire a New Tool's Config

After creating a tool, update `src/secure_agents/core/builder.py` in the `build_agent()` function. Add a mapping from your tool name to the config section it should receive:

```python
tool_configs["your_tool"] = merged.get("your_tool", {})
```

---

## Security Rules

1. **Never log document content or PII.** Use `structlog.get_logger()` and log metadata only (filenames, counts, status).
2. **Never store credentials in config.** Use `get_credential()` or `get_oauth2_token()` from `core/credentials.py`. Credentials resolve through a pluggable backend (`core/credential_backends.py`) — macOS Keychain on laptops, AES-256-GCM encrypted file unlocked with a scrypt-derived master passphrase on Linux VMs and headless servers; environment variables are always honored as a per-secret fallback. OAuth2 `client_secret` is stored in the active backend, never on disk.
3. **Use the three-layer prompt injection defense, not regex.**
   - Always pass a `response_schema` (from `core/schemas.py`) on every LLM call so the provider constrains the output shape, then re-validate the parsed JSON via `validate_schema()` as defense in depth.
   - Run untrusted text through `InputValidator.check()` (from `core/validator.py`) before it reaches the primary agent; treat any non-`safe` verdict as a hard reject and log it via the audit log.
   - Build messages with `MessageBuilder` (from `core/message_builder.py`): `add_instruction()` for trusted text, `add_untrusted(label, content)` for everything user-controlled. Never concatenate untrusted text into the system prompt.
4. **Validate files before parsing.** Call `validate_file()` from `core/security.py` to check type and size. Uses magic byte validation (not just extension checks).
5. **Use the audit log.** `AuditLog` from `core/security.py` records metadata-only events.
6. **Sandbox is enabled by default.** Docker is required. No subprocess fallback -- if Docker is missing and sandbox is enabled, it fails with a hard error. Document parsing routes through the Docker sandbox.
7. **Path traversal protection.** File storage prevents path traversal attacks. Do not construct file paths from raw user input.
8. **TLS/SSL always enforced on email.** No `use_tls`/`use_ssl` toggles -- set `allow_insecure_connections: true` to override (not recommended).
9. **Agent names validated.** Lowercase alphanumeric + underscores only.
10. **Error messages sanitized.** API responses do not leak internal details, stack traces, or credentials.
11. **Job queue DB permissions.** The SQLite job queue DB file has 0o600 permissions (owner read/write only).
12. **Dashboard hardened.** CORS restrictions, per-session auth token, binds to 127.0.0.1 only.
13. **Local providers only.** Every provider class must declare `local_only = True`. The builder enforces this -- no data ever leaves the machine.

---

## Reference Implementation

The NDA Reviewer agent at `src/secure_agents/agents/nda_reviewer/agent.py` is the canonical example. It demonstrates:
- Email polling via `email_reader` tool
- Document parsing via `document_parser` tool
- `InputValidator` screening untrusted text before analysis
- `MessageBuilder` separating system prompt, instruction, and untrusted document
- LLM analysis with `response_schema=NDA_REVIEW_SCHEMA` and re-validation via `validate_schema()`
- Report storage via `file_storage` tool
- Email reply via `email_sender` tool
- Audit logging and error handling

Study this agent before building your own.

---

## Testing Pattern

Tests live in `tests/`. Use mocks for external services:

```python
from unittest.mock import MagicMock
from secure_agents.core.base_provider import CompletionResponse

def test_my_agent():
    mock_provider = MagicMock()
    mock_provider.complete.return_value = CompletionResponse(
        content='{"result": "ok"}', model="test-model"
    )

    mock_tools = {"email_reader": MagicMock(), "document_parser": MagicMock()}
    agent = MyAgent(
        tools=mock_tools,
        provider=mock_provider,
        config={"poll_interval_seconds": 0, "validator": {"skip": True}},
    )

    # Test specific methods
    agent._process_something(test_data)
    mock_tools["document_parser"].execute.assert_called_once()
```

Set `validator.skip: true` in test configs when you don't want the InputValidator to run during isolated unit tests.

Run tests: `pytest tests/ -v`

---

## Naming Conventions

- Agent names: lowercase alphanumeric + underscores only (e.g., `nda_reviewer`, `contract_analyzer`) -- validated at registration
- Tool names: `snake_case` (e.g., `email_reader`, `slack_notifier`)
- Provider names: lowercase (e.g., `ollama`, `llamacpp`, `vllm`, `lmstudio`, `localai`, `openai_compat`)
- Agent directories: `src/secure_agents/agents/<agent_name>/`
- Tool files: `src/secure_agents/tools/<tool_name>.py`
- Config keys: `snake_case` throughout YAML
