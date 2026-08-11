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

# Tells the parent how to delegate. Delegation is the default for matched work
# (a subagent's skill only applies when the parent delegates to it), and every
# brief must be self-contained because the subagent runs in isolation — its
# ONLY input is the brief assembled from the structured ``delegate_to_<name>``
# parameters. Attached to the parent's instructions when subagents are enabled
# (``attach_delegation_protocol``).
DELEGATION_PROTOCOL = """\
## Delegation protocol

You can delegate work to subagents via `delegate_to_<name>`. Each delegate
tool's description states what it is for — treat it as its trigger. When the
task matches a subagent's described work, delegate by default instead of
doing it yourself: the subagent runs the work with its own tools and skill,
and returns a structured result. A subagent's skill only applies when you
delegate to it — doing the work yourself never reaches that skill.

Fill the structured parameters so the subagent gets a complete, self-contained
brief. It runs in isolation and sees ONLY the fields you pass — never this
conversation:

- `task` — what to achieve, decide, or produce (required).
- `scope` — the files, directories, or topics to work on.
- `constraints` — method rules (e.g. "read the file before judging", "do not
  write files", "reply in Chinese").
- `expected_output` — the required deliverable and format (list, summary,
  code; word/page caps).

Never rely on the subagent knowing anything from our conversation. After it
returns, check the delivery against `expected_output`; if a required part is
missing, either call the same subagent again with a focused follow-up task, or
state the gap in your answer. If the delivery references files, read them
before judging the result — don't judge from the summary alone.

If the delivery includes a RECOMMENDED NEXT STEP (a file to read, a subagent
to hand off to), treat it as advice — you are the router and you decide. Act
on it if it serves the user's goal; ignore or redirect it if it doesn't. If
you want a handoff suggestion, ask for it explicitly in `expected_output`
(e.g. "include a RECOMMENDED NEXT STEP") — an explicit request is more
reliable than hoping the subagent volunteers one.
"""


def _compose_brief(
    task: str,
    *,
    scope: str = "",
    constraints: str = "",
    expected_output: str = "",
) -> str:
    """Assemble the subagent's single input message from the structured fields.

    The subagent receives exactly one user message, so every provided field is
    merged into one labeled brief it can act on independently.
    """
    parts = [task]
    if scope:
        parts.append(f"Scope: {scope}")
    if constraints:
        parts.append(f"Constraints: {constraints}")
    if expected_output:
        parts.append(f"Expected output: {expected_output}")
    return "\n\n".join(parts)


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
                        "description": (
                            "What to achieve, decide, or produce. Self-contained: "
                            "the subagent cannot see this conversation."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "description": "The files, directories, or topics to work on.",
                    },
                    "constraints": {
                        "type": "string",
                        "description": (
                            "Method rules, e.g. 'read the file before judging', "
                            "'do not write files', 'reply in Chinese'."
                        ),
                    },
                    "expected_output": {
                        "type": "string",
                        "description": (
                            "The required deliverable and format — list, summary, "
                            "code; word/page caps."
                        ),
                    },
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        )
        self.subagent = subagent
        self._runner = runner
        self._model = model

    async def invoke(self, **kwargs: Any) -> ToolResult:
        task = str(kwargs.get("task") or kwargs.get("prompt") or "").strip()
        if not task:
            return ToolResult.error("no task provided to subagent", agent=self.subagent.name)
        # session_id=None => isolated history, nothing persisted. The subagent
        # gets a single user message assembled from the structured fields.
        brief = _compose_brief(
            task,
            scope=str(kwargs.get("scope") or "").strip(),
            constraints=str(kwargs.get("constraints") or "").strip(),
            expected_output=str(kwargs.get("expected_output") or "").strip(),
        )
        try:
            result = await self._runner.run(
                self.subagent.as_agent(model=self._model), brief, session_id=None
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


def add_subagents(
    agent: Agent,
    runner: Runner,
    subagents: list[Subagent],
    *,
    default_model: str | None = None,
) -> None:
    """Register every subagent as a delegation tool on ``agent``.

    ``default_model`` is the model subagents inherit (a configured cheaper
    tier); it falls back to the parent agent's model when unset. A subagent's
    own ``model`` field still wins over both.
    """
    base = default_model or agent.model
    for sa in subagents:
        agent.tools.register(subagent_as_tool(sa, runner, base))


def attach_delegation_protocol(agent: Agent) -> None:
    """Append delegation guidance to a parent agent's instructions.

    Called when subagents are enabled so the parent writes complete,
    self-contained delegation briefs instead of vague one-liners.
    """
    agent.instructions = f"{agent.instructions.rstrip()}\n\n{DELEGATION_PROTOCOL}"
