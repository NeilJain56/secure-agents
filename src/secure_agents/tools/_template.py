"""Template tool - copy this file to create a new tool.

Usage:
    cp src/secure_agents/tools/_template.py src/secure_agents/tools/your_tool.py

Then:
    1. Uncomment the @register_tool decorator below
    2. Rename the class and set name, description
    3. Implement execute() with your tool's logic
    4. Implement validate_config() to check required settings
    5. Add your tool name to an agent's tools list in config.yaml
    6. Update core/builder.py to wire your tool's config section
"""

from __future__ import annotations

from typing import Any

import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.registry import register_tool

logger = structlog.get_logger()


# Uncomment the decorator and rename the class:
# @register_tool("your_tool")
class TemplateTool(BaseTool):
    """One-line description of what this tool does."""

    name = "your_tool"
    description = "Describe what this tool does"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # Read settings from config (passed in from the agent's merged config):
        # self.api_endpoint = self.config.get("api_endpoint", "https://example.com")
        # self.timeout = int(self.config.get("timeout_seconds", 30))

    def execute(self, **kwargs: Any) -> dict:
        """Run the tool's action.

        Args:
            **kwargs: Tool-specific parameters from the calling agent.

        Returns:
            dict with results. Include "error" key on failure.
        """
        # Example implementation:
        # action = kwargs.get("action", "default")
        # logger.info("your_tool.execute", action=action)
        #
        # try:
        #     result = do_something(action)
        #     return {"status": "ok", "data": result}
        # except Exception as e:
        #     logger.error("your_tool.failed", error=str(e))
        #     return {"error": str(e)}

        return {"status": "ok"}

    def validate_config(self) -> bool:
        """Check that required config and credentials are available.

        Called by the dashboard health checks. Keep this fast.
        """
        # Example: check for required credential
        # from secure_agents.core.credentials import get_credential
        # return get_credential("your_tool_api_key") is not None
        return True
