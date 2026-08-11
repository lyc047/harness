"""Tests for subagents and manager-style orchestration."""

from __future__ import annotations

from collections.abc import AsyncIterator

from harness.agents.orchestrator import add_subagents, subagent_as_tool
from harness.agents.subagent import Subagent
from harness.core.agent import Agent
from harness.core.messages import Message, ToolCall
from harness.core.runner import Runner
from harness.llm.base import LLMResponse, StreamEnd, StreamEvent, StreamText, ToolSchema


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
    props = tool.parameters_schema["properties"]
    assert "task" in props
    # the optional structured fields are exposed so the model fills each one
    for field in ("scope", "constraints", "expected_output"):
        assert field in props and props[field]["type"] == "string"
    # the model may only fill the four structured fields
    assert tool.parameters_schema["additionalProperties"] is False
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


class _RecordingProvider:
    """Serves scripted responses and records every message list it was fed."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = script
        self.fed: list[list[Message]] = []

    async def stream(
        self, messages: list[Message], *, tools: list[ToolSchema] | None = None
    ) -> AsyncIterator[StreamEvent]:
        self.fed.append(list(messages))
        response = self.script.pop(0)
        if response.final_text:
            yield StreamText(text=response.final_text)
        yield StreamEnd(response=response)


async def test_subagent_tool_composes_structured_fields() -> None:
    """scope/constraints/expected_output are merged into the single brief the
    subagent receives — it can't see the conversation, so every provided field
    must be present in its one user message."""
    provider = _RecordingProvider([LLMResponse(final_text="done")])
    runner = Runner(provider)
    tool = subagent_as_tool(_subagent(), runner, default_model="m")

    result = await tool.invoke(
        task="Inspect the web package",
        scope="src/harness/web/",
        constraints="do not write files; reply in Chinese",
        expected_output="a 150-word summary with file paths",
    )
    assert not result.is_error
    assert result.content == "done"

    users = [m.content or "" for m in provider.fed[-1] if m.role == "user"]
    assert len(users) == 1
    assert "Inspect the web package" in users[0]
    assert "Scope: src/harness/web/" in users[0]
    assert "Constraints: do not write files" in users[0]
    assert "Expected output: a 150-word summary" in users[0]


async def test_subagent_tool_omits_absent_fields() -> None:
    """Fields the parent doesn't provide are not labeled into the brief."""
    provider = _RecordingProvider([LLMResponse(final_text="done")])
    runner = Runner(provider)
    tool = subagent_as_tool(_subagent(), runner, default_model="m")

    await tool.invoke(task="Just answer this")

    users = [m.content or "" for m in provider.fed[-1] if m.role == "user"]
    assert "Just answer this" in users[0]
    assert "Scope:" not in users[0]
    assert "Expected output:" not in users[0]


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


def test_example_subagents_include_design_and_writer() -> None:
    """The default subagent set ships the frontend-design and doc-writer
    subagents, each carrying its skill from skills/subagents/."""
    from harness.agents.examples import example_subagents

    subs = {s.name: s for s in example_subagents()}
    assert {
        "researcher",
        "coder",
        "frontend_design",
        "doc_writer",
        "search",
        "file_handler",
    } <= set(subs)

    for name, marker in {
        "frontend_design": "Frontend Design",
        "doc_writer": "Doc Co-Authoring Workflow",
    }.items():
        assert marker in subs[name].instructions, f"{name} missing its skill"


def test_subagent_skill_loads_from_bundled() -> None:
    """The subagent skills ship in the package source, so they load even on a
    fresh clone with no runtime skills/ directory. (Regression: they used to
    live only in the gitignored runtime skills/, so the test above passed only
    on machines that happened to have the files locally.)"""
    from harness.agents.examples import _load_subagent_skill

    assert "Frontend Design" in _load_subagent_skill("frontend-design")
    assert "Doc Co-Authoring Workflow" in _load_subagent_skill("doc-coauthoring")


def test_delegation_protocol_attached_to_parent() -> None:
    """When subagents are enabled, the parent is told to write self-contained
    delegation briefs (GOAL/SCOPE/CONSTRAINTS/DELIVERABLE) so an isolated
    subagent — which only receives the task string — isn't handed a vague
    one-liner."""
    from harness.agents.orchestrator import attach_delegation_protocol

    agent = Agent(name="parent", instructions="You are the parent.", model="m")
    attach_delegation_protocol(agent)
    assert "Delegation protocol" in agent.instructions
    for marker in ("task", "scope", "constraints", "expected_output"):
        assert marker in agent.instructions
    assert "isolation" in agent.instructions
    # matched work is delegated by default, not done by the parent itself
    assert "by default" in agent.instructions
    # the parent reads files the delivery references before judging the result
    assert "before judging the result" in agent.instructions


def test_subagents_carry_delivery_contract() -> None:
    """Every built-in subagent must return a structured delivery
    (WHAT YOU DID / KEY FINDINGS / GAPS) so the parent can verify the result
    even though it never sees the subagent's internals."""
    from harness.agents.examples import example_subagents

    for sa in example_subagents():
        for marker in ("WHAT YOU DID", "KEY FINDINGS", "GAPS"):
            assert marker in sa.instructions, f"{sa.name} missing {marker!r}"
        # large deliverables are written to disk and referenced by path, not
        # pasted into the reply (keeps the parent's context clean)
        assert "SAVE IT TO A FILE" in sa.instructions, (
            f"{sa.name} missing the write-to-disk rule"
        )


def test_delegate_tool_descriptions_carry_triggers() -> None:
    """Every delegate tool's description states when to use the subagent and
    makes delegation the default — the trigger is what the parent reads on
    every turn when deciding whether to delegate."""
    from harness.agents.examples import example_subagents

    for sa in example_subagents():
        assert "Use when" in sa.description, f"{sa.name} lacks a trigger condition"
        assert "by default" in sa.description, (
            f"{sa.name} does not make delegation the default"
        )
