"""Template agent - copy this directory to create a new agent.

Usage:
    cp -r src/secure_agents/agents/_template src/secure_agents/agents/your_agent

Then:
    1. Uncomment the @register_agent decorator below
    2. Rename the class and set name, description, features
    3. Implement tick() with your workflow logic
    4. Add a config entry in config.yaml under agents:
"""

from __future__ import annotations

import structlog

from secure_agents.core.base_agent import BaseAgent
from secure_agents.core.base_provider import Message
from secure_agents.core.registry import register_agent

logger = structlog.get_logger()


# Uncomment the decorator and rename the class:
# @register_agent("your_agent")
class TemplateAgent(BaseAgent):
    """One-line description of what this agent does."""

    name = "your_agent"
    description = "Describe what your agent does"
    version = "0.1.0"
    features = [
        "Feature one",
        "Feature two",
    ]

    def __init__(self, tools, provider, config=None):
        super().__init__(tools, provider, config)
        self.poll_interval = self.config.get("poll_interval_seconds", 60)

    def tick(self) -> None:
        """One iteration of work. Called in a loop by the framework.

        Implement your workflow here. Use tools and the LLM provider
        to accomplish the agent's task.
        """
        # --- Example: Read emails ---
        # email_reader = self.get_tool("email_reader")
        # result = email_reader.execute(folder="INBOX")
        # emails = result.get("emails", [])

        # --- Example: Parse a document ---
        # parser = self.get_tool("document_parser")
        # parsed = parser.execute(file_path="/path/to/file.pdf")
        # text = parsed.get("text", "")

        # --- Example: Call the LLM ---
        # messages = [
        #     Message(role="system", content="You are a helpful assistant."),
        #     Message(role="user", content=f"Analyze this: {text}"),
        # ]
        # response = self.provider.complete(messages)
        # analysis = response.content

        # --- Example: Save results ---
        # storage = self.get_tool("file_storage")
        # storage.execute(action="save", filename="report.json", data={"result": analysis})

        # --- Example: Send email ---
        # sender = self.get_tool("email_sender")
        # sender.execute(to="user@example.com", subject="Results", body=analysis)

        # Wait for next poll (use _stop_event.wait instead of time.sleep for clean shutdown)
        self._stop_event.wait(self.poll_interval)
