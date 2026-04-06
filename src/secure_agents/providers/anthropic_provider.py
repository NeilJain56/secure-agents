"""Anthropic provider - Claude API integration."""

from __future__ import annotations

import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


@register_provider("anthropic")
class AnthropicProvider(BaseProvider):
    """LLM provider using the Anthropic Claude API."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if not self.model:
            self.model = "claude-sonnet-4-20250514"
        self._client = None

    def _get_api_key(self) -> str:
        from secure_agents.core.credentials import get_credential
        key = get_credential("anthropic_api_key")
        if not key:
            raise RuntimeError(
                "No Anthropic API key found. Store it with:\n"
                "  secure-agents auth setup\n"
                "Or set the ANTHROPIC_API_KEY environment variable."
            )
        return key

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise ImportError(
                    "anthropic package not installed. "
                    "Install with: pip install 'secure-agents[anthropic]'"
                )
            self._client = anthropic.Anthropic(api_key=self._get_api_key())
        return self._client

    def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> CompletionResponse:
        client = self._get_client()
        model = self.get_model(model)
        temp = self.get_temperature(temperature)

        # Separate system message from conversation
        system_msg = ""
        conversation = []
        for m in messages:
            if m.role == "system":
                system_msg = m.content
            else:
                conversation.append({"role": m.role, "content": m.content})

        kwargs: dict = {
            "model": model,
            "max_tokens": 4096,
            "temperature": temp,
            "messages": conversation,
        }
        if system_msg:
            kwargs["system"] = system_msg

        response = client.messages.create(**kwargs)

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        return CompletionResponse(
            content=content,
            model=model,
            usage={
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
            },
            raw={"id": response.id, "stop_reason": response.stop_reason},
        )

    def is_available(self) -> bool:
        from secure_agents.core.credentials import get_credential
        if not get_credential("anthropic_api_key"):
            return False
        try:
            self._get_client()
            return True
        except Exception:
            return False
