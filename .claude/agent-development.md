# Agent Development Context for Claude Code

This file provides context for Claude Code when building new agents, tools, or providers for the Secure Agents framework. Reference this file in your Claude Code prompt with: `/read .claude/agent-development.md`

---

## Framework Architecture (What You Need to Know)

Secure Agents has three component types, all registered via decorators:

- **Agent** (`@register_agent`) — Orchestrates a workflow by composing tools + an LLM provider. Lives in `src/secure_agents/agents/<name>/agent.py`.
- **Tool** (`@register_tool`) — A reusable capability (email, parsing, storage, API calls). Lives in `src/secure_agents/tools/<name>.py`.
- **Provider** (`@register_provider`) — LLM backend (Ollama only -- local inference, no data leaves the machine). Lives in `src/secure_agents/providers/ollama.py`.

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

    def __init__(self, tools: dict, provider: BaseProvider, config: dict | None = None)
    def setup(self) -> None          # Optional hook, called once before the loop
    def tick(self) -> None           # REQUIRED. One iteration of work.
    def shutdown(self) -> None       # Optional hook, called once on stop
    def run(self) -> None            # Framework loop — do NOT override
    def request_stop(self) -> None   # Thread-safe stop signal
    def get_tool(self, name) -> BaseTool  # Lookup a tool by registered name
```

Rules:
- `tick()` is called in a loop. Do your work, then call `self._stop_event.wait(seconds)` to sleep.
- NEVER use `time.sleep()` — it blocks clean shutdown.
- Access tools via `self.get_tool("name")`, not `self.tools` directly.
- `self.config` is already deep-merged (defaults + agent overrides).
- `self.provider.complete(messages)` calls the LLM. Messages use `Message(role=, content=)`.

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
class BaseProvider(ABC):
    def complete(self, messages: list[Message], model: str = None,
                 temperature: float = None, json_mode: bool = False) -> CompletionResponse
    def is_available(self) -> bool
```

---

## Existing Tools (Reuse These)

| Tool Name | What It Does | Key kwargs |
|-----------|-------------|------------|
| `email_reader` | IMAP inbox monitor | `folder`, `mark_read`, `since_days`, `max_emails` |
| `email_sender` | SMTP email sending | `to`, `subject`, `body`, `attachments` |
| `document_parser` | PDF/DOCX text extraction | `file_path` |
| `file_storage` | JSON report save/load | `action` ("save"/"load"/"list"), `filename`, `data`, `subfolder` |

---

## Provider

Only Ollama (local inference) is supported. Cloud providers (Anthropic, OpenAI, Gemini) have been removed entirely. No data ever leaves the machine.

| Provider | Config Key | Default Model |
|----------|-----------|---------------|
| Ollama (local) | `ollama` | `llama3.2` |

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
  active: ollama                # Only supported provider
  ollama:
    host: http://localhost:11434
    model: llama3.2

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
2. **Never store credentials in config.** Use `get_credential()` or `get_oauth2_token()` from `core/credentials.py`. OAuth2 client_secret is stored in Keychain, not on disk.
3. **Sanitize text before LLM.** Call `sanitize_text()` from `core/security.py` on any user/document content before passing to the provider. Expanded to 20+ prompt injection patterns with unicode normalization.
4. **Validate files before parsing.** Call `validate_file()` from `core/security.py` to check type and size. Uses magic byte validation (not just extension checks).
5. **Use the audit log.** `AuditLog` from `core/security.py` records metadata-only events.
6. **Sandbox is enabled by default.** Docker is required. No subprocess fallback -- if Docker is missing and sandbox is enabled, it fails with a hard error. Document parsing routes through the Docker sandbox.
7. **Path traversal protection.** File storage prevents path traversal attacks. Do not construct file paths from raw user input.
8. **TLS/SSL always enforced on email.** No `use_tls`/`use_ssl` toggles -- set `allow_insecure_connections: true` to override (not recommended).
9. **Agent names validated.** Lowercase alphanumeric + underscores only.
10. **Error messages sanitized.** API responses do not leak internal details, stack traces, or credentials.
11. **Job queue DB permissions.** The SQLite job queue DB file has 0o600 permissions (owner read/write only).
12. **Dashboard hardened.** CORS restrictions, per-session auth token, binds to 127.0.0.1 only.
13. **Ollama only.** No cloud providers -- no data ever leaves the machine.

---

## Reference Implementation

The NDA Reviewer agent at `src/secure_agents/agents/nda_reviewer/agent.py` is the canonical example. It demonstrates:
- Email polling via `email_reader` tool
- Document parsing via `document_parser` tool
- LLM analysis with structured JSON output
- Report storage via `file_storage` tool
- Email reply via `email_sender` tool
- Input sanitization, audit logging, and error handling

Study this agent before building your own.

---

## Testing Pattern

Tests live in `tests/`. Use mocks for external services:

```python
from unittest.mock import MagicMock

def test_my_agent():
    mock_provider = MagicMock()
    mock_provider.complete.return_value = MagicMock(content='{"result": "ok"}')

    mock_tools = {"email_reader": MagicMock(), "document_parser": MagicMock()}
    agent = MyAgent(tools=mock_tools, provider=mock_provider, config={"poll_interval_seconds": 0})

    # Test specific methods
    agent._process_something(test_data)
    mock_tools["document_parser"].execute.assert_called_once()
```

Run tests: `pytest tests/ -v`

---

## Naming Conventions

- Agent names: lowercase alphanumeric + underscores only (e.g., `nda_reviewer`, `contract_analyzer`) -- validated at registration
- Tool names: `snake_case` (e.g., `email_reader`, `slack_notifier`)
- Provider names: lowercase (only `ollama` is supported)
- Agent directories: `src/secure_agents/agents/<agent_name>/`
- Tool files: `src/secure_agents/tools/<tool_name>.py`
- Config keys: `snake_case` throughout YAML
