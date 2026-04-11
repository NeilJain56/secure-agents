"""OpenAI-compatible local server provider.

Works with any locally-hosted server that speaks the OpenAI Chat Completions
API, including:
    - vLLM (vllm serve <model> --port 8000)
    - LM Studio (local server mode)
    - LocalAI
    - text-generation-webui (with --api flag)
    - TabbyAPI

This provider does NOT send requests to api.openai.com.  It only talks to a
``host`` that you control, and declares ``local_only = True``.  If you point
it at a remote URL, the builder's local-only check will not catch it — that
is your responsibility.  Only configure a ``host`` value that points to a
server running on your own machine or trusted on-prem infrastructure.

Structured output: uses the OpenAI-style ``response_format`` with type
``json_schema``, which is supported by vLLM (0.6+), LM Studio (0.3+), and
other recent OpenAI-compatible servers.  Providers that only support the
older ``json_object`` format fall back to that when no schema is given.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


@register_provider("openai_compat")
@register_provider("vllm")
@register_provider("lmstudio")
@register_provider("localai")
class OpenAICompatProvider(BaseProvider):
    """Provider for any local OpenAI-compatible HTTP server."""

    local_only = True

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host = config.get("host", "http://localhost:8000")
        # Must only point to local/on-prem hosts
        if self._looks_remote(self.host):
            raise ValueError(
                f"OpenAICompatProvider host '{self.host}' does not look like a "
                f"local host.  Secure Agents refuses to send data to remote APIs."
            )
        if not self.model:
            self.model = "default"

    @staticmethod
    def _looks_remote(host: str) -> bool:
        """Best-effort check that the host is local.

        Allows: localhost, 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
                192.168.0.0/16, *.local, *.internal
        Rejects: api.openai.com, any api.* hostname, etc.
        """
        import re
        from urllib.parse import urlparse

        parsed = urlparse(host)
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            return True

        local_hostnames = {"localhost", "0.0.0.0"}
        if hostname in local_hostnames:
            return False

        if hostname.endswith(".local") or hostname.endswith(".internal"):
            return False

        # IPv4 private ranges
        ipv4 = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", hostname)
        if ipv4:
            a, b = int(ipv4.group(1)), int(ipv4.group(2))
            if a == 127:
                return False
            if a == 10:
                return False
            if a == 172 and 16 <= b <= 31:
                return False
            if a == 192 and b == 168:
                return False
            return True  # other IPv4 — treat as remote

        # Anything else with a dot is suspect
        return "." in hostname

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
            "temperature": temp,
            "stream": False,
        }

        # Structured output — OpenAI-compatible response_format
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": response_schema,
                    "strict": True,
                },
            }
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = httpx.post(
            f"{self.host}/v1/chat/completions",
            json=payload,
            timeout=300.0,
        )
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        usage = data.get("usage", {})

        return CompletionResponse(
            content=content,
            model=model,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
            raw=data,
        )

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.host}/v1/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
