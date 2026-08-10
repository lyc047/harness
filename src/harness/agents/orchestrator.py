"""Manager-style orchestration: a parent agent delegates via subagent tools.

A subagent is exposed to the parent as an ordinary :class:`Tool`. When the
parent calls it, we run the subagent's own turn loop (its own instructions,
tools and isolated history) and return its final output as the tool result —
so the parent sees the subagent's answer like any other tool outcome.
"""

from __future__ import annotations

from typing import Any

from harness.agents.subagent import Subagent
from harness.core.agent import Agent
from harness.core.run_result import MaxTurnsExceeded
from harness.core.runner import Runner
from harness.observability.logging import get_logger
from harness.tools.base import Tool, ToolResult

logger = get_logger("agents")


class SubagentTool(Tool):
    """Runs a :class:`Subagent` in isolation and returns its final output."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        subagent: Subagent,
        runner: Runner,
        model: str,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            parameters_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The specific task to delegate to this subagent.",
                    }
                },
                "required": ["task"],
            },
        )
        self.subagent = subagent
        self._runner = runner
        self._model = model

    async def invoke(self, **kwargs: Any) -> ToolResult:
        task = str(kwargs.get("task") or kwargs.get("prompt") or "").strip()
        if not task:
            return ToolResult.error("no task provided to subagent", agent=self.subagent.name)
        # session_id=None => isolated history, nothing persisted.
        try:
            result = await self._runner.run(
                self.subagent.as_agent(model=self._model), task, session_id=None
            )
        except MaxTurnsExceeded as exc:
            # A delegate burning its turn budget must not crash the parent run;
            # surface it as a tool error the parent can react to.
            logger.warning("subagent %r hit %s", self.subagent.name, exc)
            return ToolResult.error(str(exc), agent=self.subagent.name)
        except Exception as exc:  # noqa: BLE001 — same: degrade, don't propagate
            logger.warning("subagent %r raised %s: %s", self.subagent.name, type(exc).__name__, exc)
            return ToolResult.error(
                f"{type(exc).__name__}: {exc}", agent=self.subagent.name
            )
        output = result.final_output or "(subagent returned no output)"
        logger.info("subagent %r completed in %d turns", self.subagent.name, result.turns)
        return ToolResult.ok(output, agent=self.subagent.name, turns=result.turns)


def subagent_as_tool(subagent: Subagent, runner: Runner, default_model: str) -> Tool:
    """Wrap a Subagent as a Tool the parent agent can call."""
    description = subagent.description or f"Delegate a subtask to the {subagent.name} subagent."
    return SubagentTool(
        name=f"delegate_to_{subagent.name}",
        description=description,
        subagent=subagent,
        runner=runner,
        model=subagent.model or default_model,
    )


def add_subagents(agent: Agent, runner: Runner, subagents: list[Subagent]) -> None:
    """Register every subagent as a delegation tool on ``agent``."""
    for sa in subagents:
        agent.tools.register(subagent_as_tool(sa, runner, agent.model))
