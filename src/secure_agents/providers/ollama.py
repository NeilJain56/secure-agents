"""Ollama provider - local LLM inference via Ollama HTTP API."""

from __future__ import annotations

import httpx
import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


@register_provider("ollama")
class OllamaProvider(BaseProvider):
    """Local LLM provider using Ollama (https://ollama.com)."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host = config.get("host", "http://localhost:11434")
        if not self.model:
            self.model = "llama3.2"

    def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> CompletionResponse:
        model = self.get_model(model)
        temp = self.get_temperature(temperature)

        payload: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temp},
        }
        if json_mode:
            payload["format"] = "json"

        response = httpx.post(
            f"{self.host}/api/chat",
            json=payload,
            timeout=300.0,
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
