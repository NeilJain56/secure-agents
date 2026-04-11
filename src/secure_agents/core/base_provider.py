"""Base provider interface for local LLM backends.

All providers implement the same interface so agents are backend-agnostic.
Only local providers are supported — no data ever leaves your machine.
Providers include Ollama, llama.cpp, vLLM, LM Studio, LocalAI, etc.

Structured output support: providers accept a ``response_schema`` (JSON Schema
dict) and constrain the LLM output to match it. This is the primary defense
against prompt injection — the model can only produce values that fit the
schema, so injected instructions cannot change the output *shape*.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """A single message in a conversation.

    Uses OpenAI-compatible role names.  The ``name`` field is optional and
    used by the message-boundary system to label data provenance (e.g.
    ``name="untrusted_document"``).
    """
    role: str          # "system", "user", "assistant"
    content: str
    name: str = ""     # optional provenance tag


@dataclass
class CompletionResponse:
    """Unified response from any local LLM provider."""
    content: str
    model: str
    usage: dict = field(default_factory=dict)   # {prompt_tokens, completion_tokens}
    raw: dict = field(default_factory=dict)     # provider-specific raw response


class BaseProvider(ABC):
    """Abstract base class for local LLM providers.

    Subclass this to add support for a new local inference backend.
    Each provider must declare ``local_only = True`` to confirm it never
    sends data off-machine.
    """

    # Subclasses MUST set this to True. The builder verifies it.
    local_only: bool = True

    def __init__(self, config: dict) -> None:
        self.config = config
        self.model = config.get("model", "")
        self.temperature = config.get("temperature", 0.1)

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        response_schema: dict[str, Any] | None = None,
    ) -> CompletionResponse:
        """Send messages to the LLM and return a completion.

        Args:
            messages: Conversation history as Message objects.
            model: Override the default model for this call.
            temperature: Override the default temperature for this call.
            json_mode: Request JSON output. Providers that support structured
                       output natively should use ``response_schema`` instead.
            response_schema: A JSON Schema dict. When provided the provider
                             SHOULD constrain the LLM output to match this
                             schema.  If the backend doesn't support native
                             schema enforcement, the provider must still set
                             ``json_mode=True`` and the framework will validate
                             the output against the schema after the call.

        Returns:
            CompletionResponse with the model's reply.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is reachable and ready."""
        ...

    def get_model(self, override: str | None = None) -> str:
        return override or self.model

    def get_temperature(self, override: float | None = None) -> float:
        return override if override is not None else self.temperature
