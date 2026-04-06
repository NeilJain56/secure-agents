# Adding a New Agent

This guide walks through creating a new agent for the Secure Agents framework. The framework is general-purpose -- agents can automate any workflow (email triage, document analysis, compliance monitoring, research, etc.).

## Step 1: Create the Agent Directory

```bash
mkdir -p src/secure_agents/agents/your_agent
touch src/secure_agents/agents/your_agent/__init__.py
```

## Step 2: Implement the Agent

Create `src/secure_agents/agents/your_agent/agent.py`:

```python
import structlog

from secure_agents.core.base_agent import BaseAgent
from secure_agents.core.base_provider import Message
from secure_agents.core.registry import register_agent

logger = structlog.get_logger()


@register_agent("your_agent")
class YourAgent(BaseAgent):
    name = "your_agent"
    description = "Describe what your agent does"

    def __init__(self, tools, provider, config=None):
        super().__init__(tools, provider, config)
        self.poll_interval = self.config.get("poll_interval_seconds", 60)

    def tick(self):
        # This runs in a loop. Implement your workflow here.

        # Use tools:
        email_reader = self.get_tool("email_reader")
        result = email_reader.execute(folder="INBOX")

        # Use the LLM provider:
        messages = [
            Message(role="system", content="You are a helpful assistant."),
            Message(role="user", content="Analyze this document..."),
        ]
        response = self.provider.complete(messages)

        # Read agent-specific settings from your merged config:
        max_size = self.config.get("security", {}).get("max_file_size_mb", 50)

        # Use _stop_event.wait() instead of time.sleep() for clean shutdown
        self._stop_event.wait(self.poll_interval)
```

## Step 3: Add Configuration

Add your agent to `config.yaml`. Your agent inherits everything from `defaults` and can override whatever it needs:

```yaml
agents:
  your_agent:
    enabled: true
    poll_interval_seconds: 120
    tools:
      - email_reader      # Reuse existing tools
      - document_parser
      - file_storage
    # Override any default for THIS agent only:
    security:
      max_file_size_mb: 200       # This agent handles larger files
      allowed_file_types: [.pdf]  # Only PDFs
    storage:
      output_dir: ./output/your_agent  # Separate output dir
    # Optionally use a different LLM provider:
    provider:
      override: anthropic
      model: claude-sonnet-4-20250514
```

**You don't need to touch `defaults` or any other agent's config.** Your agent gets its own isolated settings.

## Step 4: Run It

```bash
# Run just your agent
secure-agents start your_agent

# Run alongside other agents in parallel
secure-agents start your_agent nda_reviewer

# Or enable it and start all enabled agents
secure-agents start
```

## Available Tools

These tools are already registered and can be reused by any agent:

| Tool | Name | Description |
|------|------|-------------|
| Email Reader | `email_reader` | Monitor IMAP inbox, download attachments |
| Email Sender | `email_sender` | Send emails via SMTP |
| Document Parser | `document_parser` | Extract text from PDF/DOCX |
| File Storage | `file_storage` | Save/load JSON reports locally |

Each agent gets its own tool instances with its own config, so two agents can use `document_parser` with different file size limits.

## Available Providers

All providers share the same interface. Your agent doesn't need to know which one is active:

- `ollama` - Local inference (default)
- `anthropic` - Claude API
- `openai` - GPT API
- `gemini` - Gemini API

The global default is set in `provider.active`. An individual agent can override it with `provider.override` in its config section.

## Tips

- **Keep agents thin** - Put workflow logic in `tick()`, delegate I/O to tools
- **Reuse tools** - Don't reimplement email or document parsing
- **Use `_stop_event.wait()`** instead of `time.sleep()` so agents shut down cleanly
- **Use the provider interface** - Don't import provider SDKs directly
- **Log metadata only** - Never log document content or PII
- **Override only what you need** - Your agent inherits all defaults; only specify what's different
