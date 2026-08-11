"""Tests for human-in-the-loop: permissions, approval, pause/resume."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.core.agent import Agent
from harness.core.messages import Message, ToolCall
from harness.core.run_result import RunPaused, RunState
from harness.core.runner import RunDone, Runner
from harness.llm.base import LLMResponse
from harness.memory.session import SessionStore
from harness.safety.approver import ApprovalExecutor, Mode
from harness.safety.permissions import Permission, Permissions, Rule
from harness.tools.base import ToolResult


def _tc(name: str = "bash", args: str = "{}") -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=args)


# -- permissions engine --

def test_default_harness_read_allowed_bash_asks() -> None:
    perms = Permissions.default_harness()
    assert perms.decide(_tc("read_file")) is Permission.ALLOW
    # Rules must match the builtin tool names exactly (not the "glob"/"grep"
    # shorthand — fnmatch without a wildcard is an exact match, so the
    # shorthand silently fell through to ASK and broke plan-mode reads).
    assert perms.decide(_tc("glob_files")) is Permission.ALLOW
    assert perms.decide(_tc("grep_files")) is Permission.ALLOW
    assert perms.decide(_tc("bash")) is Permission.ASK
    assert perms.decide(_tc("mcp_server_tool")) is Permission.ASK


def test_deny_beats_ask_and_allow() -> None:
    perms = Permissions(
        default=Permission.ALLOW,
        rules=[
            Rule("write_file", Permission.ALLOW),
            Rule("bash", Permission.DENY),
        ],
    )
    # allow rule matches, but deny rule wins regardless of order
    assert perms.decide(_tc("bash")) is Permission.DENY
    assert perms.decide(_tc("write_file")) is Permission.ALLOW


def test_glob_tool_pattern() -> None:
    perms = Permissions(
        rules=[Rule("mcp_*", Permission.ASK), Rule("bash", Permission.ALLOW)]
    )
    assert perms.decide(_tc("mcp_demo_add", '{"a": 1}')) is Permission.ASK
    assert perms.decide(_tc("bash")) is Permission.ALLOW


def test_arg_pattern_matches() -> None:
    perms = Permissions(
        default=Permission.ALLOW,
        rules=[Rule("bash", Permission.DENY, pattern="rm -rf")],
    )
    assert perms.decide(_tc("bash", '{"command": "rm -rf /tmp/x"}')) is Permission.DENY
    assert perms.decide(_tc("bash", '{"command": "ls"}')) is Permission.ALLOW


def test_from_config_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "perms.toml"
    cfg.write_text(
        'default = "ask"\n'
        '[[rules]]\n'
        'tool = "bash"\n'
        'permission = "deny"\n'
        'pattern = "rm -rf"\n',
        encoding="utf-8",
    )
    perms = Permissions.from_config(cfg)
    assert perms.default is Permission.ASK
    assert perms.decide(_tc("bash", '{"command": "rm -rf /"}')) is Permission.DENY
    assert perms.decide(_tc("bash", '{"command": "ls"}')) is Permission.ASK


def test_to_toml_roundtrip(tmp_path: Path) -> None:
    perms = Permissions(rules=[Rule("bash", Permission.DENY, pattern="rm")])
    path = tmp_path / "out.toml"
    path.write_text(perms.to_toml(), encoding="utf-8")
    loaded = Permissions.from_config(path)
    assert loaded.decide(_tc("bash", '{"command": "rm -rf /"}')) is Permission.DENY


# -- approval executor --

async def _make_executor(
    choice: str = "y", *, inner: object | None = None
) -> tuple[ApprovalExecutor, list[ToolCall]]:
    """Build an ApprovalExecutor over a recording inner executor."""
    calls: list[ToolCall] = []

    async def default_inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        calls.append(tool_call)
        return ToolResult.ok("ok")

    actual: object = inner or default_inner

    async def prompt(tc: ToolCall) -> str:
        return choice

    perms = Permissions(rules=[Rule("bash", Permission.ASK)])
    executor = ApprovalExecutor(actual, perms, prompt=prompt)  # type: ignore[arg-type]
    return executor, calls


async def test_approval_allow() -> None:
    executor, calls = await _make_executor("y")
    result = await executor(Agent(name="a", instructions="i", model="m"), _tc())
    assert not result.is_error
    assert len(calls) == 1


async def test_approval_deny_blocks_and_reports() -> None:
    executor, calls = await _make_executor("n")
    result = await executor(Agent(name="a", instructions="i", model="m"), _tc())
    assert result.is_error
    assert "blocked by user" in result.content
    assert calls == []  # inner never ran


async def test_approval_edit_args() -> None:
    async def prompt(tc: ToolCall) -> str:
        return 'e:{"command": "echo edited"}'

    calls: list[ToolCall] = []

    async def inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        calls.append(tool_call)
        return ToolResult.ok(tool_call.arguments)

    executor = ApprovalExecutor(
        inner, Permissions(rules=[Rule("bash", Permission.ASK)]), prompt=prompt
    )  # type: ignore[arg-type]
    result = await executor(Agent(name="a", instructions="i", model="m"), _tc())
    assert not result.is_error
    assert result.content == '{"command": "echo edited"}'


async def test_approval_bad_edited_json_blocks() -> None:
    async def prompt(tc: ToolCall) -> str:
        return "e:{not json"

    async def inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        raise AssertionError("inner must not run for invalid edit")

    executor = ApprovalExecutor(
        inner, Permissions(rules=[Rule("bash", Permission.ASK)]), prompt=prompt
    )  # type: ignore[arg-type]
    result = await executor(Agent(name="a", instructions="i", model="m"), _tc())
    assert result.is_error
    assert "not valid JSON" in result.content


async def test_approval_allow_for_session() -> None:
    executor, calls = await _make_executor("a")
    agent = Agent(name="a", instructions="i", model="m")
    await executor(agent, _tc())  # "a" -> allowed for session
    await executor(agent, _tc())  # auto-allowed, no prompt
    assert len(calls) == 2


async def test_approval_pause_signal() -> None:
    async def prompt(tc: ToolCall) -> str:
        return "p"

    paused: list[bool] = []

    async def inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        return ToolResult.ok("ok")

    executor = ApprovalExecutor(
        inner,
        Permissions(rules=[Rule("bash", Permission.ASK)]),
        prompt=prompt,
        on_pause=lambda: paused.append(True),
    )  # type: ignore[arg-type]
    result = await executor(Agent(name="a", instructions="i", model="m"), _tc())
    assert not result.is_error
    assert paused == [True]


async def test_approval_no_prompt_fails_closed() -> None:
    async def inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        raise AssertionError("must not run without an approver")

    executor = ApprovalExecutor(inner, Permissions(rules=[Rule("bash", Permission.ASK)]))  # type: ignore[arg-type]
    result = await executor(Agent(name="a", instructions="i", model="m"), _tc())
    assert result.is_error
    assert "no approver" in result.content


# -- permission modes --

def _make_mode_executor(
    perms: Permissions,
    *,
    mode: Mode = Mode.ASK,
    inner: object | None = None,
) -> tuple[ApprovalExecutor, list[ToolCall], list[ToolCall]]:
    """Build an ApprovalExecutor over a recording inner; track prompt calls too."""
    calls: list[ToolCall] = []
    prompts: list[ToolCall] = []

    async def recording_inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        calls.append(tool_call)
        return ToolResult.ok("ok")

    async def recording_prompt(tc: ToolCall) -> str:
        prompts.append(tc)
        return "y"

    executor = ApprovalExecutor(
        inner or recording_inner,  # type: ignore[arg-type]
        perms,
        prompt=recording_prompt,
        mode=mode,
    )
    return executor, calls, prompts


def _agent() -> Agent:
    return Agent(name="a", instructions="i", model="m")


async def test_mode_auto_auto_approves_ask() -> None:
    executor, calls, prompts = _make_mode_executor(
        Permissions(rules=[Rule("bash", Permission.ASK)]), mode=Mode.AUTO
    )
    result = await executor(_agent(), _tc())
    assert not result.is_error
    assert len(calls) == 1
    assert prompts == []


async def test_mode_auto_respects_deny() -> None:
    executor, calls, prompts = _make_mode_executor(
        Permissions(rules=[Rule("bash", Permission.DENY)]), mode=Mode.AUTO
    )
    result = await executor(_agent(), _tc())
    assert result.is_error
    assert "denied" in result.content
    assert calls == []
    assert prompts == []


async def test_mode_plan_reads_allow_mutations_denied() -> None:
    executor, calls, prompts = _make_mode_executor(
        Permissions.default_harness(), mode=Mode.PLAN
    )
    # read_file is explicitly allowed by the default policy → runs without a prompt
    result = await executor(_agent(), _tc("read_file"))
    assert not result.is_error
    assert len(calls) == 1
    # write_file is only ASK under the policy → plan mode blocks it
    result = await executor(_agent(), _tc("write_file"))
    assert result.is_error
    assert "denied" in result.content
    assert len(calls) == 1
    assert prompts == []


async def test_mode_plan_allows_read_search_tools() -> None:
    """Plan mode stays read-only but must still permit the search tools the
    policy allows unconditionally (glob_files / grep_files under
    default_harness) — read-only planning is unusable if searching asks."""
    executor, calls, prompts = _make_mode_executor(
        Permissions.default_harness(), mode=Mode.PLAN
    )
    for name in ("read_file", "glob_files", "grep_files"):
        result = await executor(_agent(), _tc(name))
        assert not result.is_error, name
    assert len(calls) == 3
    assert prompts == []


async def test_mode_plan_respects_explicit_allow() -> None:
    executor, calls, _prompts = _make_mode_executor(
        Permissions(rules=[Rule("bash", Permission.ALLOW)]), mode=Mode.PLAN
    )
    result = await executor(_agent(), _tc())
    assert not result.is_error
    assert len(calls) == 1


async def test_mode_full_overrides_deny() -> None:
    executor, calls, prompts = _make_mode_executor(
        Permissions(rules=[Rule("bash", Permission.DENY)]), mode=Mode.FULL
    )
    result = await executor(_agent(), _tc())
    assert not result.is_error
    assert len(calls) == 1
    assert prompts == []


async def test_set_mode_switches_behavior() -> None:
    executor, calls, prompts = _make_mode_executor(
        Permissions(rules=[Rule("bash", Permission.ASK)])
    )
    assert executor.mode is Mode.ASK
    result = await executor(_agent(), _tc())
    assert not result.is_error
    assert len(prompts) == 1  # ASK mode prompts

    executor.set_mode(Mode.AUTO)
    assert executor.mode is Mode.AUTO
    result = await executor(_agent(), _tc())
    assert not result.is_error
    assert len(prompts) == 1  # no new prompt
    assert len(calls) == 2


# -- pause / resume --

async def test_runner_pause_and_resume(make_provider) -> None:
    provider = make_provider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 2}')]),
            LLMResponse(final_text="3"),
        ]
    )
    runner = Runner(provider, pause_check=lambda s: s.turns >= 1)

    from harness.tools.base import tool
    from harness.tools.registry import ToolRegistry

    @tool
    async def add(a: int, b: int) -> int:
        return a + b

    registry = ToolRegistry()
    registry.register(add)
    agent = Agent(name="t", instructions="sys", tools=registry, max_turns=5)

    with pytest.raises(RunPaused) as excinfo:
        async for _ in runner.run_streamed(agent, "1+2?"):
            pass
    state = excinfo.value.state
    assert state.turns == 1

    final: str | None = None
    async for event in runner.resume_streamed(agent, state):
        if isinstance(event, RunDone):
            final = event.result.final_output
    assert final == "3"
    assert final is not None and "tool" in [m.role for m in state.messages]


def test_runstate_json_roundtrip() -> None:
    state = RunState(
        messages=[Message.user("hi"), Message.assistant("yo", reasoning_content="think")],
        turns=3,
        max_turns=10,
        session_id="s1",
    )
    restored = RunState.from_json(state.to_json())
    assert restored.turns == 3
    assert restored.max_turns == 10
    assert restored.session_id == "s1"
    assert restored.messages[0].content == "hi"
    assert restored.messages[1].reasoning_content == "think"


async def test_checkpoint_store_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "safety.db"))
    await store.initialize()
    session = await store.create_session()

    state = RunState(
        messages=[Message.user("x")], turns=2, max_turns=5, session_id=session.id
    )
    cid = await store.save_checkpoint(state)
    loaded = await store.load_checkpoint(cid)
    assert loaded is not None
    assert loaded.turns == 2
    assert loaded.session_id == session.id
    assert loaded.messages[0].content == "x"

    listed = await store.list_checkpoints()
    assert any(cid == c[0] for c in listed)

    await store.delete_checkpoint(cid)
    assert await store.load_checkpoint(cid) is None
    await store.close()
