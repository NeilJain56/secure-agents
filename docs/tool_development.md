# Tool Development Guide

This guide covers creating new tools for the Secure Agents framework. Tools are reusable, self-contained units of functionality shared across agents.

## BaseTool Interface

Every tool extends `BaseTool` from `secure_agents.core.base_tool`:

```python
class BaseTool(ABC):
    name: str = ""          # Unique identifier, matches registry key
    description: str = ""   # Human-readable summary

    def __init__(self, config: dict | None = None) -> None
    def execute(self, **kwargs) -> dict    # ABSTRACT - run the tool's action
    def validate_config(self) -> bool      # Check required config is present
```

- `execute()` receives keyword arguments and returns a dict with results.
- `validate_config()` returns `True` if the tool has everything it needs. Called by the dashboard health checks.
- `config` is a dict passed in during agent build. Each agent gets its own tool instance with its own config, so two agents using the same tool can have different settings.

## Minimal Working Example

```python
import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.registry import register_tool

logger = structlog.get_logger()


@register_tool("my_tool")
class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something useful"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.some_setting = self.config.get("some_setting", "default_value")

    def execute(self, **kwargs) -> dict:
        action = kwargs.get("action", "default")
        logger.info("my_tool.execute", action=action)

        # Do the work here...
        result = {"status": "ok", "action": action}

        return result

    def validate_config(self) -> bool:
        # Return False if required config is missing
        return bool(self.some_setting)
```

## Config Schema

Tool config comes from the merged agent config at build time. The wiring happens in `core/builder.py`:

```python
# In builder.py, tool configs are extracted from the merged agent config:
tool_configs = {
    "email_reader": email_cfg.get("imap", {}),
    "email_sender": email_cfg.get("smtp", {}),
    "document_parser": security_cfg,
    "file_storage": storage_cfg,
}
```

To wire your tool's config, either:

1. **Use an existing config section** (e.g., `security`, `storage`, `email.imap`) -- update `builder.py` to map your tool name to the appropriate section.
2. **Add a custom section** -- add a new key under `defaults` in `config.yaml` and update `builder.py` to pass it through.

Example config section for a custom tool:

```yaml
defaults:
  my_tool:
    api_endpoint: https://example.com/api
    timeout_seconds: 30
    max_results: 100
```

Then in `builder.py`, add:
```python
tool_configs["my_tool"] = merged.get("my_tool", {})
```

## Declaring Required Credentials

Tools should never read credentials from config files. Use the credential system in `core/credentials.py`:

```python
from secure_agents.core.credentials import get_credential, get_oauth2_token

class MyTool(BaseTool):
    def execute(self, **kwargs):
        # Read from macOS Keychain or environment variable
        api_key = get_credential("my_tool_api_key")
        if not api_key:
            return {"error": "No API key found. Run: secure-agents auth setup"}

        # For OAuth2-based services:
        token = get_oauth2_token(self.config.get("username", ""))
        ...

    def validate_config(self) -> bool:
        # Check that the credential is available
        return get_credential("my_tool_api_key") is not None
```

Credential lookup order:
1. macOS Keychain (via `keyring` library, service name `"secure-agents"`)
2. Environment variable (uppercase version of the key, e.g., `MY_TOOL_API_KEY`)

Users store credentials with `secure-agents auth setup` or by setting env vars.

## Implementing Connection Testing

The dashboard calls `validate_config()` during health checks. If your tool connects to an external service, implement a lightweight connection test:

```python
def validate_config(self) -> bool:
    api_key = get_credential("my_tool_api_key")
    if not api_key:
        return False

    # Optionally test the connection
    try:
        import httpx
        resp = httpx.get(
            f"{self.config.get('api_endpoint')}/health",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        return resp.status_code == 200
    except Exception:
        return False
```

Keep `validate_config()` fast -- it runs on every dashboard refresh.

## Exposing Tool Functionality to Agents

Agents access tools via `self.get_tool("tool_name")` and call `execute(**kwargs)`:

```python
# Inside an agent's tick() method:
my_tool = self.get_tool("my_tool")
result = my_tool.execute(action="search", query="something")

if "error" in result:
    logger.warning("my_tool failed", error=result["error"])
else:
    data = result["data"]
    # Process data...
```

Conventions for `execute()` return values:
- Always return a dict.
- Include an `"error"` key (string) on failure so agents can detect problems.
- Use descriptive keys for success results.
- Never include raw credentials or PII in return values.
- Error messages are sanitized in API responses -- do not expose internal paths, stack traces, or credentials in error dicts.

## Best Practices for Credential Handling

1. **Never store credentials in config files.** Use `get_credential()` from `core/credentials.py`. OAuth2 client_secret is stored in Keychain, not on disk.
2. **Never log credentials.** Use structlog and log metadata only.
3. **Fail clearly.** If a credential is missing, return a helpful error message telling the user how to set it up. Note: error messages in API responses are sanitized -- do not leak internal details.
4. **Validate early.** Check credentials in `validate_config()` so the dashboard can show the problem before the agent starts.
5. **Support both Keychain and env vars.** `get_credential()` handles this automatically -- just pick a consistent key name.
6. **No cloud provider keys.** Only Ollama (local inference) is supported. There are no cloud API keys (Anthropic, OpenAI, Gemini) to manage.

## Security Considerations for Tools

- **File validation uses magic bytes.** `validate_file()` in `core/security.py` checks magic bytes (not just file extensions) to verify file types. Always call it before processing uploaded or received files.
- **Path traversal protection.** File storage has path traversal protection built in. Do not construct file paths by concatenating user input -- use the `file_storage` tool or the security utilities.
- **Sandbox execution.** Sandbox is enabled by default and requires Docker. Document parsing routes through the Docker sandbox when `sandbox_enabled=True`. If Docker is missing and sandbox is enabled, it fails with a hard error (no subprocess fallback).
- **Input sanitization.** `sanitize_text()` in `core/security.py` detects 20+ prompt injection patterns with unicode normalization. Always sanitize user/document content before passing to the provider.
- **TLS/SSL on email.** TLS/SSL is always enforced on email connections. There are no `use_tls`/`use_ssl` toggles -- you must set `allow_insecure_connections: true` to override.

## Template File

A ready-to-copy template is at `src/secure_agents/tools/_template.py`. Copy it and follow the comments:

```bash
cp src/secure_agents/tools/_template.py src/secure_agents/tools/your_tool.py
```

After copying:
1. Uncomment the `@register_tool` decorator
2. Set `name` and `description`
3. Implement `execute()` and `validate_config()`
4. Add your tool to an agent's `tools` list in `config.yaml`
5. Update `builder.py` if your tool needs config from a specific section
