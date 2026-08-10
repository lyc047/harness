"""Tests for subagents and manager-style orchestration."""

from __future__ import annotations

from harness.agents.orchestrator import add_subagents, subagent_as_tool
from harness.agents.subagent import Subagent
from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import Runner
from harness.llm.base import LLMResponse


def _subagent(name: str = "worker") -> Subagent:
    return Subagent(
        name=name,
        instructions=f"{name} instructions",
        description=f"Delegate work to {name}.",
    )


def test_subagent_as_agent() -> None:
    sa = _subagent()
    agent = sa.as_agent(model="inherited-model")
    assert agent.name == "worker"
    assert agent.instructions == "worker instructions"
    assert agent.model == "inherited-model"
    assert agent.max_turns == 10
    # subagent with no model inherits when a parent model is given
    assert sa.as_agent().model == ""
    assert sa.as_agent("parent-model").model == "parent-model"


def test_subagent_as_tool_wiring(make_provider) -> None:
    runner = Runner(make_provider())
    tool = subagent_as_tool(_subagent(), runner, default_model="parent-model")
    assert tool.name == "delegate_to_worker"
    assert tool.parameters_schema["required"] == ["task"]
    assert "task" in tool.parameters_schema["properties"]
    # empty model on the subagent inherits the parent model
    assert tool._model == "parent-model"


def test_add_subagents_registers_delegation_tools(make_provider) -> None:
    agent = Agent(name="parent", instructions="parent", model="m")
    runner = Runner(make_provider())
    add_subagents(agent, runner, [_subagent("a"), _subagent("b")])
    assert set(agent.tools.names()) == {"delegate_to_a", "delegate_to_b"}


async def test_parent_delegates_to_subagent(make_provider) -> None:
    """Parent calls delegate_to_worker; the subagent runs isolated and the
    parent's final answer incorporates the subagent's output."""
    script = [
        # parent turn 1 -> delegate
        LLMResponse(
            tool_calls=[
                ToolCall(id="tc1", name="delegate_to_worker", arguments='{"task": "research X"}')
            ]
        ),
        # subagent turn 1 -> answer directly
        LLMResponse(final_text="research result for X"),
        # parent turn 2 -> wrap up
        LLMResponse(final_text="Combined answer."),
    ]
    provider = make_provider(script)
    agent = Agent(name="parent", instructions="parent", model="m")
    runner = Runner(provider)
    add_subagents(agent, runner, [_subagent()])

    result = await runner.run(agent, "Do the thing", session_id=None)
    assert result.final_output == "Combined answer."

    # the tool message fed back to the parent carried the subagent's output
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert tool_msgs and "research result for X" in tool_msgs[-1].content or ""

    # isolation: parent turn 1 -> subagent -> parent turn 2
    assert provider.stream_calls == [2, 2, 4]
    assert len(result.messages) == 5


async def test_subagent_tool_missing_task_is_error(make_provider) -> None:
    provider = make_provider()
    runner = Runner(provider)
    tool = subagent_as_tool(_subagent(), runner, default_model="m")
    result = await tool.invoke()
    assert result.is_error
    assert "no task" in result.content
    assert provider.stream_calls == []  # nothing ran


async def test_subagent_max_turns_degrades_not_crash(make_provider) -> None:
    """A delegate exhausting its turn budget returns an error ToolResult
    instead of propagating MaxTurnsExceeded into the parent's run."""
    script = [LLMResponse(tool_calls=[ToolCall(id="t1", name="x", arguments="{}")])]
    runner = Runner(make_provider(script))
    sa = _subagent()
    sa.max_turns = 1  # one tool-call turn, then budget exhausted
    tool = subagent_as_tool(sa, runner, default_model="m")
    result = await tool.invoke(task="do it")
    assert result.is_error
    assert "max_turns" in result.content
