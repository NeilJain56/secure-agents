"""Ollama provider — local LLM inference via the Ollama HTTP API.

Supports structured output via ``response_schema``: when a JSON Schema is
provided, it is passed to Ollama's ``format`` parameter so the model's
output is grammar-constrained to match the schema.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


@register_provider("ollama")
class OllamaProvider(BaseProvider):
    """Local LLM provider using Ollama (https://ollama.com).

    Ollama >= 0.5 supports ``format: <json-schema>`` to constrain the model
    output to a specific JSON schema.  When ``response_schema`` is supplied,
    this provider uses that mechanism.  When only ``json_mode=True`` is set
    (no schema), it falls back to ``format: "json"``.
    """

    local_only = True

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host = config.get("host", "http://localhost:11434")
        if not self.model:
            self.model = "llama3.2"

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

        payload: dict = {
            "model": model,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages
            ],
            "stream": False,
            "options": {
                "temperature": temp,
                # Cap output tokens so structured-output calls never stall.
                # Our JSON outputs have: category/is_similar key (~5 tok),
                # confidence (~5 tok), reasoning up to 300 chars (~75 tok),
                # plus JSON syntax (~20 tok) = ~105 tokens max.
                # Benchmarked at 100: always produces valid JSON, ~8-10s/call.
                # 150 was safe but wasted ~2s of generation per call.
                "num_predict": 100,
            },
        }

        # Structured output: schema takes precedence over plain json_mode
        if response_schema is not None:
            payload["format"] = response_schema
        elif json_mode:
            payload["format"] = "json"

        response = httpx.post(
            f"{self.host}/api/chat",
            json=payload,
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()

        return CompletionResponse(
            content=data["message"]["content"],
            model=model,
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
            raw=data,
        )

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.host}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
