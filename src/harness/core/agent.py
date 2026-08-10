"""Agent: immutable configuration for a run.

An Agent never executes anything itself — the :class:`Runner` drives it.
This mirrors the openai-agents-python design: Agent is a data carrier, Runner
is a stateless executor, so an Agent instance can be reused safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.tools.registry import ToolRegistry


@dataclass
class Agent:
    name: str
    instructions: str
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    model: str = ""
    max_turns: int = 30
    model_settings: dict[str, Any] = field(default_factory=dict)

    def tool_schemas(self) -> list[dict[str, Any]] | None:
        """Function schemas for the LLM request, or None if no tools."""
        schemas = self.tools.to_function_schemas()
        return schemas or None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Agent {self.name} tools={self.tools.names()}>"
