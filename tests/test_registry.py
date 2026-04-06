"""Tests for the plugin registry."""

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.base_provider import BaseProvider, Message, CompletionResponse
from secure_agents.core.registry import Registry


def test_register_and_retrieve_tool():
    reg = Registry()

    class DummyTool(BaseTool):
        name = "dummy"
        description = "A dummy tool"
        def execute(self, **kwargs):
            return {"result": "ok"}

    reg.register_tool("dummy", DummyTool)
    assert reg.get_tool_class("dummy") is DummyTool
    assert "dummy" in reg.list_tools()


def test_create_tool_with_config():
    reg = Registry()

    class ConfigTool(BaseTool):
        name = "cfg"
        description = "Config test"
        def execute(self, **kwargs):
            return {"limit": self.config.get("max_size", 0)}

    reg.register_tool("cfg", ConfigTool)
    tool1 = reg.create_tool("cfg", {"max_size": 50})
    tool2 = reg.create_tool("cfg", {"max_size": 200})
    # Different configs produce independent instances
    assert tool1.config["max_size"] == 50
    assert tool2.config["max_size"] == 200
    assert tool1 is not tool2


def test_resolve_tools():
    reg = Registry()

    class ToolA(BaseTool):
        name = "a"
        description = "A"
        def execute(self, **kwargs):
            return {}

    class ToolB(BaseTool):
        name = "b"
        description = "B"
        def execute(self, **kwargs):
            return {}

    reg.register_tool("a", ToolA)
    reg.register_tool("b", ToolB)

    tools = reg.resolve_tools(["a", "b"])
    assert set(tools.keys()) == {"a", "b"}
    assert isinstance(tools["a"], ToolA)
    assert isinstance(tools["b"], ToolB)


def test_register_provider():
    reg = Registry()

    class DummyProvider(BaseProvider):
        def complete(self, messages, model=None, temperature=None, json_mode=False):
            return CompletionResponse(content="hello", model="dummy")
        def is_available(self):
            return True

    reg.register_provider("dummy", DummyProvider)
    assert reg.get_provider("dummy") is DummyProvider


def test_missing_tool_raises():
    reg = Registry()
    try:
        reg.get_tool_class("nonexistent")
        assert False, "Should have raised KeyError"
    except KeyError:
        pass


def test_missing_agent_raises():
    reg = Registry()
    try:
        reg.get_agent("nonexistent")
        assert False, "Should have raised KeyError"
    except KeyError:
        pass
