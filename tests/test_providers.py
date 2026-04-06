"""Tests for the provider system."""

from secure_agents.core.base_provider import BaseProvider, CompletionResponse, Message
from secure_agents.core.registry import Registry


class MockProvider(BaseProvider):
    """A mock provider for testing."""

    def __init__(self, config=None):
        super().__init__(config or {"model": "mock-model", "temperature": 0.5})
        self.calls = []

    def complete(self, messages, model=None, temperature=None, json_mode=False):
        self.calls.append({
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "json_mode": json_mode,
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
    response = provider.complete(messages, model="custom-model")
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
