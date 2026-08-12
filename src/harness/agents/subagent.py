"""Subagent: an isolated agent another agent can delegate to."""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.core.agent import Agent
from harness.tools.base import Tool
from harness.tools.registry import ToolRegistry


@dataclass
class Subagent:
    """A self-contained agent config, runnable as a tool by a parent agent.

    Each delegation runs in a fresh, isolated context: the subagent never sees
    the parent's history, and its own history is discarded after the call —
    this is what keeps subagent work from polluting the main conversation.
    """

    name: str
    instructions: str
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    model: str = ""  # empty => inherit the parent's model
    max_turns: int = 10
    description: str = ""  # guides the parent on when to delegate

    def as_agent(
        self,
        model: str = "",
        extra_tools: tuple[Tool, ...] = (),
        extra_instructions: str = "",
    ) -> Agent:
        """Materialise as a runnable :class:`Agent` (empty model inherits later).

        ``extra_tools`` (nested delegate tools for advanced mode) register onto
        a *copy* of the subagent's registry so the shared ``Subagent.tools`` is
        never mutated. ``extra_instructions`` append after the subagent's own.
        """
        tools = ToolRegistry()
        for t in self.tools.all():
            tools.register(t)
        for t in extra_tools:
            tools.register(t)
        instructions = self.instructions
        if extra_instructions:
            instructions = f"{instructions.rstrip()}\n\n{extra_instructions}"
        return Agent(
            name=self.name,
            instructions=instructions,
            tools=tools,
            model=model or self.model,
            max_turns=self.max_turns,
        )
