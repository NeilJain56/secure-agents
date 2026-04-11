"""llama.cpp provider — local inference via the llama.cpp HTTP server.

Run the server with:
    ./llama-server -m /path/to/model.gguf --port 8080 --json-schema-to-grammar

The llama.cpp server's /completion endpoint accepts a ``json_schema`` field
that constrains output via GBNF grammar.  This provider uses that mechanism
when ``response_schema`` is supplied.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


def _messages_to_prompt(messages: list[Message]) -> str:
    """Flatten messages to a single prompt for /completion.

    llama.cpp also supports /v1/chat/completions (OpenAI-compatible) but
    schema constraints are cleanest via /completion + json_schema.
    """
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"<|system|>\n{m.content}\n")
        elif m.role == "user":
            parts.append(f"<|user|>\n{m.content}\n")
        elif m.role == "assistant":
            parts.append(f"<|assistant|>\n{m.content}\n")
    parts.append("<|assistant|>\n")
    return "".join(parts)


@register_provider("llamacpp")
class LlamaCppProvider(BaseProvider):
    """Local LLM provider using the llama.cpp HTTP server."""

    local_only = True

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host = config.get("host", "http://localhost:8080")
        if not self.model:
            self.model = "default"

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        response_schema: dict[str, Any] | None = None,
    ) -> CompletionResponse:
        model = self.get_model(model)
        temp = self.get_temperature(temperature)
        prompt = _messages_to_prompt(messages)

        payload: dict = {
            "prompt": prompt,
            "temperature": temp,
            "n_predict": 4096,
            "stream": False,
            "cache_prompt": True,
        }

        # Structured output — llama.cpp accepts a JSON Schema directly
        if response_schema is not None:
            payload["json_schema"] = response_schema
        elif json_mode:
            payload["json_schema"] = {"type": "object"}

        response = httpx.post(
            f"{self.host}/completion",
            json=payload,
            timeout=300.0,
        )
        response.raise_for_status()
        data = response.json()

        return CompletionResponse(
            content=data.get("content", ""),
            model=model,
            usage={
                "prompt_tokens": data.get("tokens_evaluated", 0),
                "completion_tokens": data.get("tokens_predicted", 0),
            },
            raw=data,
        )

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.host}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
