"""Tests for the provider system."""

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import Registry


class MockProvider(BaseProvider):
    """A mock provider for testing."""

    local_only = True

    def __init__(self, config=None):
        super().__init__(config or {"model": "mock-model", "temperature": 0.5})
        self.calls = []

    def complete(self, messages, *, model=None, temperature=None, json_mode=False, response_schema=None):
        self.calls.append({
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "json_mode": json_mode,
            "response_schema": response_schema,
        })
        return CompletionResponse(
            content='{"result": "mock response"}',
            model=self.get_model(model),
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )

    def is_available(self):
        return True


def test_provider_complete():
    provider = MockProvider()
    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Hello"),
    ]
    response = provider.complete(messages)
    assert response.content == '{"result": "mock response"}'
    assert response.model == "mock-model"
    assert len(provider.calls) == 1


def test_provider_model_override():
    provider = MockProvider()
    messages = [Message(role="user", content="test")]
    provider.complete(messages, model="custom-model")
    assert provider.calls[0]["model"] == "custom-model"


def test_provider_temperature_override():
    provider = MockProvider()
    assert provider.get_temperature() == 0.5
    assert provider.get_temperature(0.9) == 0.9


def test_provider_registration():
    reg = Registry()
    reg.register_provider("mock", MockProvider)
    cls = reg.get_provider("mock")
    instance = cls({"model": "test", "temperature": 0.1})
    assert instance.is_available()


def test_provider_response_schema_forwarded():
    """The response_schema kwarg must be passed through to the provider."""
    provider = MockProvider()
    schema = {"type": "object", "required": ["x"]}
    provider.complete([Message(role="user", content="hi")], response_schema=schema)
    assert provider.calls[0]["response_schema"] is schema


def test_provider_declares_local_only():
    """All built-in providers must declare local_only=True."""
    from secure_agents.providers.ollama import OllamaProvider
    from secure_agents.providers.llamacpp import LlamaCppProvider
    from secure_agents.providers.openai_compat import OpenAICompatProvider

    assert OllamaProvider.local_only is True
    assert LlamaCppProvider.local_only is True
    assert OpenAICompatProvider.local_only is True
