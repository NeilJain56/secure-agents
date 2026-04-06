"""Base provider interface for LLM backends.

All providers implement the same interface so agents are backend-agnostic.
Swapping from Ollama to Gemini is a config change, not a code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Message:
    """A single message in a conversation. Uses OpenAI-compatible format."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class CompletionResponse:
    """Unified response from any LLM provider."""
    content: str
    model: str
    usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens}
    raw: dict = field(default_factory=dict)     # Provider-specific raw response


class BaseProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.model = config.get("model", "")
        self.temperature = config.get("temperature", 0.1)

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> CompletionResponse:
        """Send messages to the LLM and return a completion.

        Args:
            messages: Conversation history as Message objects.
            model: Override the default model.
            temperature: Override the default temperature.
            json_mode: Request structured JSON output if supported.

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
