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


async def test_subagent_uses_its_own_provider(make_provider) -> None:
    """When a subagent_provider is wired, the nested run talks to that account
    while the parent talks to its own — per-subagent API key tiering."""
    parent_provider = make_provider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="tc1", name="delegate_to_worker", arguments='{"task": "research X"}'
                    )
                ]
            ),
            LLMResponse(final_text="Combined answer."),
        ]
    )
    sub_provider = make_provider([LLMResponse(final_text="research result for X")])

    agent = Agent(name="parent", instructions="parent", model="m")
    runner = Runner(parent_provider)
    add_subagents(agent, runner, [_subagent()], subagent_provider=sub_provider)

    result = await runner.run(agent, "Do the thing", session_id=None)
    assert result.final_output == "Combined answer."
    # parent's two turns went to the parent provider, the subagent's to its own
    assert len(parent_provider.stream_calls) == 2
    assert len(sub_provider.stream_calls) == 1


async def test_subagent_without_own_provider_shares_parent(make_provider) -> None:
    """No subagent_provider => subagents run on the parent's provider (the
    default before per-subagent accounts existed)."""
    parent_provider = make_provider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="tc1", name="delegate_to_worker", arguments='{"task": "research X"}'
                    )
                ]
            ),
            LLMResponse(final_text="research result for X"),
            LLMResponse(final_text="Combined answer."),
        ]
    )
    agent = Agent(name="parent", instructions="parent", model="m")
    runner = Runner(parent_provider)
    add_subagents(agent, runner, [_subagent()])

    result = await runner.run(agent, "Do the thing", session_id=None)
    assert result.final_output == "Combined answer."
    # subagent turn consumed a stream from the SAME provider
    assert len(parent_provider.stream_calls) == 3


async def test_parent_routes_to_second_subagent(make_provider) -> None:
    """After one subagent delivers, the parent can hand off to a different one —
    the collaboration is parent-routed (the parent reads RECOMMENDED NEXT STEP
    and decides), not a subagent-to-subagent message."""
    script = [
        LLMResponse(
            tool_calls=[ToolCall(id="t1", name="delegate_to_a", arguments='{"task": "research"}')]
        ),
        # subagent a delivers, suggesting the next step
        LLMResponse(final_text="KEY FINDINGS: x. RECOMMENDED NEXT STEP: delegate to b."),
        # the parent follows the suggestion and delegates to a second subagent
        LLMResponse(
            tool_calls=[ToolCall(id="t2", name="delegate_to_b", arguments='{"task": "implement"}')]
        ),
        LLMResponse(final_text="B done"),
        LLMResponse(final_text="final answer"),
    ]
    provider = make_provider(script)
    agent = Agent(name="parent", instructions="parent", model="m")
    runner = Runner(provider)
    add_subagents(agent, runner, [_subagent("a"), _subagent("b")])

    result = await runner.run(agent, "Do it", session_id=None)
    assert result.final_output == "final answer"

    # both delegates ran, in handoff order; subagent runs stay isolated
    called = [tc.name for m in result.messages for tc in (m.tool_calls or [])]
    assert called == ["delegate_to_a", "delegate_to_b"]


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
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
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
        "coder": "Coding Discipline",
        "security_reviewer": "Security Review",
    }.items():
        assert marker in subs[name].instructions, f"{name} missing its skill"


def test_subagent_skill_loads_from_bundled() -> None:
    """The subagent skills ship in the package source, so they load even on a
    fresh clone with no runtime skills/ directory. (Regression: they used to
    live only in the gitignored runtime skills/, so the test above passed only
    on machines that happened to have the files locally.)"""
    from harness.agents.registry import load_subagent_skill

    assert "Frontend Design" in load_subagent_skill("frontend-design")
    assert "Doc Co-Authoring Workflow" in load_subagent_skill("doc-coauthoring")
    assert "Coding Discipline" in load_subagent_skill("coding")
    assert "Security Review" in load_subagent_skill("security-review")


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
    # the parent decides on the subagent's RECOMMENDED NEXT STEP (router role)
    assert "router" in agent.instructions
    # ...and it can request a next-step recommendation explicitly, because the
    # contract only encourages the subagent to volunteer one
    assert "ask for it explicitly" in agent.instructions


def test_subagents_carry_delivery_contract() -> None:
    """Every built-in subagent must return a structured delivery
    (WHAT YOU DID / KEY FINDINGS / GAPS) so the parent can verify the result
    even though it never sees the subagent's internals."""
    from harness.agents.examples import example_subagents

    for sa in example_subagents():
        for marker in ("WHAT YOU DID", "KEY FINDINGS", "RECOMMENDED NEXT STEP", "GAPS"):
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


def test_security_reviewer_is_readonly_and_skilled() -> None:
    """security_reviewer ships read-only and carries the security-review skill."""
    from pathlib import Path

    from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry
    from harness.tools.builtin import builtin_registry

    reg = SubagentRegistry(Path(".") / "nope", bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("security_reviewer")
    assert spec is not None
    assert spec.skill == "security-review"
    assert "write_file" not in spec.tools and "bash" not in spec.tools
    sa = reg.to_subagent(spec)
    assert "Security Review" in sa.instructions
    # every declared tool name resolves to a builtin (registry tolerates unknowns,
    # so assert the allowlist actually binds)
    builtins = builtin_registry()
    for name in spec.tools:
        assert builtins.get(name) is not None, f"unknown tool {name!r}"


# ---- YAML subagent registry ---- #


def test_subagent_registry_loads_bundled_defaults(tmp_path) -> None:
    """The six defaults ship as YAML configs in the package source, so a fresh
    clone gets them even with no runtime skills/ directory."""
    from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry

    reg = SubagentRegistry(tmp_path / "empty", bundled_dir=BUNDLED_SUBAGENTS_DIR)
    specs = reg.discover()
    assert {
        "researcher",
        "coder",
        "frontend_design",
        "doc_writer",
        "search",
        "file_handler",
    } <= {s.name for s in specs}

    researcher = reg.get("researcher")
    assert researcher is not None
    assert "Use when" in researcher.description
    sa = reg.to_subagent(researcher)
    assert sa.name == "researcher"
    assert sa.max_turns == 8
    # the delivery contract is appended uniformly by the materializer
    assert "WHAT YOU DID" in sa.instructions


def test_subagent_registry_runtime_overrides_bundled(tmp_path) -> None:
    """A same-named YAML in the runtime skills/subagents dir wins over the
    bundled default (mirrors SkillRegistry's override layering)."""
    from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry

    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "researcher.yaml").write_text(
        "name: researcher\n"
        "description: Use when X; delegate by default.\n"
        "instructions: |\n"
        "  Custom research body.\n"
        "max_turns: 4\n",
        encoding="utf-8",
    )

    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    reg.discover()
    researcher = reg.get("researcher")
    assert researcher is not None
    assert researcher.description == "Use when X; delegate by default."
    assert researcher.max_turns == 4
    sa = reg.to_subagent(researcher)
    assert "Custom research body." in sa.instructions
    assert "WHAT YOU DID" in sa.instructions  # contract still appended


def test_subagent_registry_adds_new_subagent_from_yaml(tmp_path) -> None:
    """A brand-new YAML config registers a subagent with zero Python changes —
    the point of making the registry declarative."""
    from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry

    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "translator.yaml").write_text(
        "name: translator\n"
        "description: Use when text needs translating; delegate by default.\n"
        "instructions: |\n"
        "  Translate the given text faithfully.\n"
        "max_turns: 3\n",
        encoding="utf-8",
    )

    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    reg.discover()
    spec = reg.get("translator")
    assert spec is not None
    sa = reg.to_subagent(spec)
    assert sa.name == "translator"
    assert sa.max_turns == 3
    assert "Translate the given text faithfully." in sa.instructions
    assert "WHAT YOU DID" in sa.instructions


def test_subagent_registry_ignores_invalid_yaml(tmp_path) -> None:
    """A malformed or non-dict YAML file is skipped, not fatal."""
    from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry

    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    (runtime / "scalar.yaml").write_text("just a string\n", encoding="utf-8")

    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    reg.discover()
    assert "broken" not in reg.names()
    assert "scalar" not in reg.names()
    # bundled defaults still load alongside
    assert "researcher" in reg.names()


# ---- per-subagent model tiering ---- #


async def test_runner_passes_agent_model_to_provider(make_provider) -> None:
    """The provider now honors ``agent.model`` per request — previously the
    runner never forwarded it, so per-agent models were cosmetic."""
    provider = make_provider([LLMResponse(final_text="done")])
    agent = Agent(name="assistant", instructions="x", model="deepseek-v4-pro")
    runner = Runner(provider)
    await runner.run(agent, "hi", session_id=None)
    assert provider.models == ["deepseek-v4-pro"]


def test_subagent_model_tiering_wiring(make_provider) -> None:
    """The configured cheaper tier is inherited by every delegate unless a
    subagent pins its own model (which wins)."""
    runner = Runner(make_provider())
    agent = Agent(name="parent", instructions="p", model="parent-model")
    subs = [_subagent("a"), _subagent("b")]
    subs[1].model = "custom-model"
    add_subagents(agent, runner, subs, default_model="cheap-model")
    tool_a = agent.tools.get("delegate_to_a")
    tool_b = agent.tools.get("delegate_to_b")
    assert tool_a is not None and tool_a._model == "cheap-model"
    assert tool_b is not None and tool_b._model == "custom-model"


# ---- per-run subagent turn budget ---- #


def test_subagent_budget_tracks_and_resets() -> None:
    from harness.agents.orchestrator import SubagentBudget

    b = SubagentBudget(total=10)
    assert b.remaining() == 10
    b.record(3)
    assert b.remaining() == 7
    b.record(8)
    assert b.remaining() == -1  # over-run is recorded, not clamped
    b.reset()
    assert b.remaining() == 10


# ---- advanced mode: nested delegation, budget enforcement ---- #


def test_advanced_add_subagents_builds_nested_delegates(make_provider) -> None:
    from harness.agents.orchestrator import add_subagents

    agent = Agent(name="parent", instructions="p", model="m")
    runner = Runner(make_provider())
    add_subagents(agent, runner, [_subagent("a"), _subagent("b")], advanced=True)
    assert set(agent.tools.names()) == {"delegate_to_a", "delegate_to_b"}
    tool_a = agent.tools.get("delegate_to_a")
    assert tool_a is not None
    # every OTHER subagent is a nested delegate; never itself
    assert sorted(t.name for t in tool_a._nested_delegates) == ["delegate_to_b"]
    # nested delegates carry no further delegates (structural depth cap of 2)
    nested_b = tool_a._nested_delegates[0]
    assert nested_b._nested_delegates == ()
    assert nested_b._concurrent is True
    assert tool_a._concurrent is True


def test_attach_delegation_protocol_swaps_variants() -> None:
    from harness.agents.orchestrator import (
        DELEGATION_PROTOCOL,
        DELEGATION_PROTOCOL_ADVANCED,
        attach_delegation_protocol,
    )

    agent = Agent(name="parent", instructions="base", model="m")
    attach_delegation_protocol(agent)
    assert agent.instructions.count("Delegation protocol") == 1
    assert agent.instructions.endswith(DELEGATION_PROTOCOL)
    attach_delegation_protocol(agent, advanced=True)
    assert agent.instructions.count("Delegation protocol") == 1  # swapped, not appended
    assert agent.instructions.endswith(DELEGATION_PROTOCOL_ADVANCED)
    attach_delegation_protocol(agent)
    assert agent.instructions.count("Delegation protocol") == 1
    assert agent.instructions.endswith(DELEGATION_PROTOCOL)


async def test_advanced_nested_delegation_two_levels(make_provider) -> None:
    """parent -> a -> b: level-2 subagent runs inside the level-1 subagent's
    isolated stream, results bubble back through the delegate chain."""
    from harness.agents.orchestrator import add_subagents

    script = [
        LLMResponse(
            tool_calls=[ToolCall(id="p1", name="delegate_to_a", arguments='{"task": "outer"}')]
        ),
        LLMResponse(
            tool_calls=[ToolCall(id="a1", name="delegate_to_b", arguments='{"task": "inner"}')]
        ),
        LLMResponse(final_text="B delivered"),
        LLMResponse(final_text="A delivered"),
        LLMResponse(final_text="parent done"),
    ]
    provider = make_provider(script)
    agent = Agent(name="parent", instructions="p", model="m")
    runner = Runner(provider)
    add_subagents(agent, runner, [_subagent("a"), _subagent("b")], advanced=True)

    result = await runner.run(agent, "go", session_id=None)
    assert result.final_output == "parent done"


async def test_subagent_budget_exhausted_returns_error(make_provider) -> None:
    from harness.agents.orchestrator import SubagentBudget, subagent_as_tool

    budget = SubagentBudget(total=1)
    runner = Runner(make_provider())
    tool = subagent_as_tool(
        _subagent(), runner, default_model="m", budget=budget, advanced=True
    )
    assert not (await tool.invoke(task="x")).is_error
    budget.record(1)
    denied = await tool.invoke(task="y")
    assert denied.is_error
    assert "budget exhausted" in denied.content
    # nothing ran for the denied call


async def test_subagent_escalates_to_fallback_model(make_provider) -> None:
    """A subagent that errors (burns its turn budget) is re-dispatched once on
    the fallback model; both attempts stream, and the escalation is surfaced as
    a SubagentEscalated event."""
    from harness.agents.orchestrator import SubagentEscalated, subagent_as_tool
    from harness.agents.subagent import Subagent

    script = [
        # attempt 1 (flash, max_turns=1): a tool call burns the only turn
        LLMResponse(tool_calls=[ToolCall(id="s1", name="some_tool", arguments="{}")]),
        # attempt 2 (pro): succeeds
        LLMResponse(final_text="escalated result"),
    ]
    provider = make_provider(script)
    runner = Runner(provider)
    events: list[object] = []

    async def sink(run_id: str, agent: str, event: object) -> None:
        events.append(event)

    tool = subagent_as_tool(
        Subagent(
            name="worker", instructions="worker instructions",
            description="Delegate work to worker.", max_turns=1,
        ),
        runner,
        default_model="flash",
        fallback_model="pro",
        on_event=sink,
    )
    result = await tool.invoke(task="do it")
    assert not result.is_error
    assert "escalated result" in result.content
    # first attempt on the cheap model, escalated attempt on the fallback
    assert provider.models == ["flash", "pro"]
    assert any(isinstance(e, SubagentEscalated) and e.model == "pro" for e in events)


async def test_subagent_no_escalation_without_fallback(make_provider) -> None:
    """Without fallback_model a failed attempt surfaces the error directly —
    no second dispatch."""
    from harness.agents.orchestrator import subagent_as_tool
    from harness.agents.subagent import Subagent

    provider = make_provider(
        [LLMResponse(tool_calls=[ToolCall(id="s1", name="some_tool", arguments="{}")])]
    )
    tool = subagent_as_tool(
        Subagent(
            name="worker", instructions="worker instructions",
            description="Delegate work to worker.", max_turns=1,
        ),
        Runner(provider),
        default_model="flash",
    )
    result = await tool.invoke(task="do it")
    assert result.is_error
    assert provider.models == ["flash"]  # exactly one attempt, no escalation


# ---- #3: explicit contract (acceptance criteria in the brief + machine hook) ---- #


async def test_contract_appended_to_subagent_brief() -> None:
    """Explicit acceptance criteria (#3) are appended to every brief the
    subagent receives, so it sees the bar before it starts."""
    provider = _RecordingProvider([LLMResponse(final_text="done")])
    tool = subagent_as_tool(
        _subagent(), Runner(provider), default_model="m",
        contract="ruff must be clean; pytest must pass; report real output",
    )

    result = await tool.invoke(task="Implement the module")
    assert not result.is_error

    users = [m.content or "" for m in provider.fed[-1] if m.role == "user"]
    assert len(users) == 1
    assert "Contract — your output is judged against these criteria:" in users[0]
    assert "ruff must be clean" in users[0]


async def test_contract_violation_triggers_escalation(make_provider) -> None:
    """A check_contract hook that flags a non-compliant output treats it as a
    failed attempt: the subagent escalates to the fallback model once, and the
    escalated attempt is re-checked (#3)."""
    from harness.agents.orchestrator import SubagentEscalated, subagent_as_tool
    from harness.agents.subagent import Subagent

    def check(output: str) -> str | None:
        return None if "COMPLIANT" in output else "missing COMPLIANT marker"

    provider = make_provider(
        [
            LLMResponse(final_text="first attempt, not compliant"),
            LLMResponse(final_text="second attempt COMPLIANT"),
        ]
    )
    events: list[object] = []

    async def sink(run_id: str, agent: str, event: object) -> None:
        events.append(event)

    tool = subagent_as_tool(
        Subagent(
            name="worker", instructions="worker instructions",
            description="Delegate work to worker.", max_turns=2,
        ),
        Runner(provider),
        default_model="flash",
        fallback_model="pro",
        check_contract=check,
        on_event=sink,
    )
    result = await tool.invoke(task="produce a compliant answer")
    assert not result.is_error
    assert "COMPLIANT" in result.content
    # first attempt on the cheap model, escalated attempt on the fallback
    assert provider.models == ["flash", "pro"]
    assert any(isinstance(e, SubagentEscalated) and e.model == "pro" for e in events)


# ---- #2: task-type-aware model routing ---- #


def test_classify_subtask_hints() -> None:
    """classify_subtask tiers: design-heavy subagent name -> pro; brief-text
    reasoning hints -> pro; everything else -> default ("")."""
    from harness.agents.routing import classify_subtask

    # name-based (the subagent's whole job is design/reasoning/analysis)
    assert classify_subtask("frontend_design", "build a page", "") == "pro"
    assert classify_subtask("security_reviewer", "check the code", "") == "pro"
    assert classify_subtask("researcher", "look something up", "") == "pro"
    # keyword-based — normally-mechanical type, reasoning-heavy brief
    assert classify_subtask("coder", "Design the data model", "") == "pro"
    assert classify_subtask("coder", "review this function", "") == "pro"
    assert classify_subtask("coder", "", "architecture of the module") == "pro"
    # default — no reason to burn a pro token
    assert classify_subtask("coder", "add a sum function", "") == ""
    assert classify_subtask("search", "find the file", "") == ""


async def test_task_router_routes_design_subtask_to_pro(make_provider) -> None:
    """A design-heavy subagent (frontend_design) is routed to the pro model for
    its first attempt, not the flash default (#2)."""
    from harness.agents.orchestrator import subagent_as_tool
    from harness.agents.routing import make_task_router
    from harness.agents.subagent import Subagent

    provider = make_provider([LLMResponse(final_text="design delivered")])
    tool = subagent_as_tool(
        Subagent(
            name="frontend_design", instructions="design UI",
            description="Delegate UI design.", model="flash", max_turns=2,
        ),
        Runner(provider),
        default_model="flash",
        router=make_task_router(pro_model="pro"),
    )
    result = await tool.invoke(task="Design the landing page UI")
    assert not result.is_error
    assert provider.models == ["pro"]


async def test_task_router_keeps_mechanical_on_default(make_provider) -> None:
    """A mechanical coder task with no reasoning hints stays on the flash
    default even with a router wired (#2)."""
    from harness.agents.orchestrator import subagent_as_tool
    from harness.agents.routing import make_task_router
    from harness.agents.subagent import Subagent

    provider = make_provider([LLMResponse(final_text="code delivered")])
    tool = subagent_as_tool(
        Subagent(
            name="coder", instructions="write code",
            description="Delegate coding.", model="flash", max_turns=2,
        ),
        Runner(provider),
        default_model="flash",
        router=make_task_router(pro_model="pro"),
    )
    result = await tool.invoke(task="Add a function to compute the sum")
    assert not result.is_error
    assert provider.models == ["flash"]


async def test_router_routed_failure_does_not_escalate_same_model(make_provider) -> None:
    """When the router already bumped the first attempt to pro, a failure must
    NOT trigger a second pro dispatch via the fallback — the escalation guard
    compares against the model actually used (#2)."""
    from harness.agents.orchestrator import subagent_as_tool
    from harness.agents.routing import make_task_router
    from harness.agents.subagent import Subagent

    provider = make_provider(
        [LLMResponse(tool_calls=[ToolCall(id="s1", name="some_tool", arguments="{}")])]
    )
    tool = subagent_as_tool(
        Subagent(
            name="frontend_design", instructions="design UI",
            description="Delegate UI design.", model="flash", max_turns=1,
        ),
        Runner(provider),
        default_model="flash",
        fallback_model="pro",  # same model the router would pick — no double dispatch
        router=make_task_router(pro_model="pro"),
    )
    result = await tool.invoke(task="Design a component")
    assert result.is_error
    assert provider.models == ["pro"]  # exactly one attempt
