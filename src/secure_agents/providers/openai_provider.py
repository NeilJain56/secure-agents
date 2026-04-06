"""OpenAI provider - GPT API integration."""

from __future__ import annotations

import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


@register_provider("openai")
class OpenAIProvider(BaseProvider):
    """LLM provider using the OpenAI API."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if not self.model:
            self.model = "gpt-4o"
        self._client = None

    def _get_api_key(self) -> str:
        from secure_agents.core.credentials import get_credential
        key = get_credential("openai_api_key")
        if not key:
            raise RuntimeError(
                "No OpenAI API key found. Store it with:\n"
                "  secure-agents auth setup\n"
                "Or set the OPENAI_API_KEY environment variable."
            )
        return key

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise ImportError(
                    "openai package not installed. "
                    "Install with: pip install 'secure-agents[openai]'"
                )
            self._client = openai.OpenAI(api_key=self._get_api_key())
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

        oai_messages = [{"role": m.role, "content": m.content} for m in messages]

        kwargs: dict = {
            "model": model,
            "messages": oai_messages,
            "temperature": temp,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        return CompletionResponse(
            content=choice.message.content or "",
            model=model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
            },
            raw={"id": response.id, "finish_reason": choice.finish_reason},
        )

    def is_available(self) -> bool:
        from secure_agents.core.credentials import get_credential
        if not get_credential("openai_api_key"):
            return False
        try:
            self._get_client()
            return True
        except Exception:
            return False
