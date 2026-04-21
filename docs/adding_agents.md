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
from secure_agents.core.message_builder import MessageBuilder
from secure_agents.core.registry import register_agent
from secure_agents.core.schemas import validate_schema
from secure_agents.core.validator import InputValidator

logger = structlog.get_logger()

YOUR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
    },
    "required": ["summary", "score"],
    "additionalProperties": False,
}


@register_agent("your_agent")
class YourAgent(BaseAgent):
    name = "your_agent"
    description = "Describe what your agent does"

    def __init__(self, tools, provider, config=None, **kwargs):
        super().__init__(tools, provider, config, **kwargs)
        self.poll_interval = self.config.get("poll_interval_seconds", 60)
        validator_cfg = (self.config.get("validator") or {})
        self._validator = None if validator_cfg.get("skip") else InputValidator(
            provider,
            confidence_threshold=validator_cfg.get("confidence_threshold", 0.7),
        )

    def tick(self):
        # This runs in a loop. Implement your workflow here.
        email_reader = self.get_tool("email_reader")
        result = email_reader.execute(folder="INBOX")

        document_text = result.get("text", "")

        # Layer 2: screen untrusted text with the validator (fails closed).
        if self._validator is not None:
            verdict = self._validator.check(document_text)
            if verdict.verdict != "safe":
                logger.warning("validator_rejected", reasons=verdict.reasons)
                return

        # Layer 3: build messages with explicit boundaries — system prompt
        # NEVER contains user-controlled text.
        messages = (
            MessageBuilder("You are a helpful assistant. Analyze the document.")
            .add_instruction("Return JSON with `summary` and `score`.")
            .add_untrusted("document", document_text)
            .build()
        )

        # Layer 1: structured output schema constrains the LLM response.
        response = self.provider.complete(messages, response_schema=YOUR_RESPONSE_SCHEMA)

        ok, parsed = validate_schema(response.content, YOUR_RESPONSE_SCHEMA)
        if not ok:
            logger.warning("schema_validation_failed", error=parsed)
            return

        # parsed is now a dict matching YOUR_RESPONSE_SCHEMA
        logger.info("analyzed", score=parsed["score"])

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
```

**You don't need to touch `defaults` or any other agent's config.** Your agent gets its own isolated settings. Note: only local LLM backends are supported (Ollama, llama.cpp, vLLM, LM Studio, LocalAI) and every provider declares `local_only = True`, so no data ever leaves the machine.

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

## Provider

Pluggable local LLM backends. Cloud providers (Anthropic, OpenAI, Gemini) have been removed -- all inference stays on-machine. Built-in backends: `ollama`, `llamacpp`, `vllm`, `lmstudio`, `localai`, and the generic `openai_compat` for any OpenAI-compatible local server. Each provider class declares `local_only = True` and the builder rejects anything else.

The global provider is set via `provider.active`. An agent can pick a different provider just for itself via `agents.<name>.provider.override` (with optional `model`, `temperature`, `host`).

## Multi-Agent Sequencing

Agents can hand off work to other agents via a shared job queue. The builder automatically passes a `JobQueue` instance to every agent — you never create one yourself. Call `self.emit()` to enqueue a job for another agent:

```python
# In your agent's tick() method:

# Sequential handoff: pass the result to agent B
self.emit("agent_b", {"document": "nda.pdf", "risk_score": 7})

# Parallel fan-out: notify both B and C
self.emit("summarizer", {"text": extracted_text})
self.emit("archiver", {"file": filepath})
```

Rules:
- **Always use `self.emit()`** — never call `self.job_queue.enqueue()` directly. `emit()` adds logging and handles the `None` queue case as a silent no-op.
- **Accept `**kwargs` in `__init__`** and pass them through to `super().__init__()` so the `job_queue` keyword is forwarded:
  ```python
  def __init__(self, tools, provider, config=None, **kwargs):
      super().__init__(tools, provider, config, **kwargs)
  ```
- Consuming agents use `self.job_queue.dequeue(self.name)` to pull pending jobs.

## Tips

- **Keep agents thin** - Put workflow logic in `tick()`, delegate I/O to tools
- **Reuse tools** - Don't reimplement email or document parsing
- **Use `_stop_event.wait()`** instead of `time.sleep()` so agents shut down cleanly
- **Use the provider interface** - Call `self.provider.complete(messages, response_schema=...)` rather than importing a backend directly
- **Always pass `response_schema`** - Structured outputs are the first layer of injection defense; never make a free-form `complete()` call
- **Use `MessageBuilder` for untrusted text** - Never concatenate document content into a system prompt
- **Run untrusted text through `InputValidator`** - It fails closed; treat any non-`safe` verdict as a hard reject
- **Use `self.emit()` to hand off work** - Never call `self.job_queue.enqueue()` directly; `emit()` is a safe no-op when no queue is wired
- **Log metadata only** - Never log document content or PII
- **Override only what you need** - Your agent inherits all defaults; only specify what's different
- **Agent names must be valid** - Lowercase alphanumeric characters and underscores only
- **Sandbox is on by default** - Docker is required; if Docker is missing and sandbox is enabled, the agent will fail with a hard error
