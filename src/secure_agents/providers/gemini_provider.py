"""Google Gemini provider - Gemini API integration."""

from __future__ import annotations

import structlog

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import register_provider

logger = structlog.get_logger()


@register_provider("gemini")
class GeminiProvider(BaseProvider):
    """LLM provider using the Google Gemini API."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if not self.model:
            self.model = "gemini-2.5-flash"
        self._client = None

    def _get_api_key(self) -> str:
        from secure_agents.core.credentials import get_credential
        key = get_credential("gemini_api_key")
        if not key:
            raise RuntimeError(
                "No Gemini API key found. Store it with:\n"
                "  secure-agents auth setup\n"
                "Or set the GEMINI_API_KEY environment variable."
            )
        return key

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError:
                raise ImportError(
                    "google-genai package not installed. "
                    "Install with: pip install 'secure-agents[gemini]'"
                )
            self._client = genai.Client(api_key=self._get_api_key())
        return self._client

    def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> CompletionResponse:
        from google.genai import types

        client = self._get_client()
        model = self.get_model(model)
        temp = self.get_temperature(temperature)

        # Separate system instruction from conversation
        system_instruction = None
        contents = []
        for m in messages:
            if m.role == "system":
                system_instruction = m.content
            else:
                role = "user" if m.role == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part(text=m.content)]))

        config = types.GenerateContentConfig(
            temperature=temp,
        )
        if system_instruction:
            config.system_instruction = system_instruction
        if json_mode:
            config.response_mime_type = "application/json"

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        content = response.text or ""
        usage = {}
        if response.usage_metadata:
            usage = {
                "prompt_tokens": response.usage_metadata.prompt_token_count or 0,
                "completion_tokens": response.usage_metadata.candidates_token_count or 0,
            }

        return CompletionResponse(
            content=content,
            model=model,
            usage=usage,
            raw={},
        )

    def is_available(self) -> bool:
        from secure_agents.core.credentials import get_credential
        if not get_credential("gemini_api_key"):
            return False
        try:
            self._get_client()
            return True
        except Exception:
            return False
