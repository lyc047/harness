"""Unit tests for the per-connection web runtime (WebApprover + Runtime)."""

from __future__ import annotations

import asyncio
import json

import pytest

from harness.config import Settings
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.llm.base import LLMResponse
from harness.memory.store import Store
from harness.tools.base import ToolResult
from harness.web.runtime import Runtime, WebApprover, build_runtime


def _settings(tmp_path: object, **overrides: str) -> Settings:
    base = {
        "HARNESS_DB_PATH": str(tmp_path / "harness.db"),  # type: ignore[operator]
        "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),  # type: ignore[operator]
        "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),  # type: ignore[operator]
    }
    base.update(overrides)
    return Settings.from_env(base)


async def _collect_frames(
    rt: Runtime,
    *,
    until: object | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Drain the outbox until ``until`` matches a frame type (or forever)."""
    frames: list[dict] = []
    while True:
        raw = await asyncio.wait_for(rt.outbox.get(), timeout=timeout)
        frame = json.loads(raw)
        frames.append(frame)
        if until is None or frame["type"] == until:
            return frames


async def _make_runtime(
    tmp_path: object,
    make_provider: object,
    script: list[LLMResponse] | None = None,
    *,
    tool_executor: ToolExecutor | None = None,
    **env: str,
) -> tuple[Runtime, Store]:
    settings = _settings(tmp_path, **env)
    store = Store(settings)
    await store.initialize()
    provider = make_provider(script)  # type: ignore[operator]
    rt = build_runtime(settings, store, provider=provider, tool_executor=tool_executor)
    await rt.start()
    return rt, store


# ---- WebApprover ---- #


def _tool_call() -> ToolCall:
    return ToolCall(id="t1", name="bash", arguments='{"command": "ls"}')


async def test_web_approver_returns_allow_once() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    decisions: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox, decisions)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    decisions.put_nowait("y")
    assert await task == "y"
    frame = json.loads(outbox.get_nowait())
    assert frame["type"] == "approval_required"
    assert frame["tool_call"]["name"] == "bash"


async def test_web_approver_deny() -> None:
    outbox, decisions = asyncio.Queue(), asyncio.Queue()
    approver = WebApprover(outbox, decisions)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    decisions.put_nowait("n")
    assert await task == "n"


async def test_web_approver_edit_args_passthrough() -> None:
    outbox, decisions = asyncio.Queue(), asyncio.Queue()
    approver = WebApprover(outbox, decisions)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    edited = 'e:{"command": "pwd"}'
    decisions.put_nowait(edited)
    assert await task == edited


async def test_web_approver_timeout_fails_closed() -> None:
    outbox, decisions = asyncio.Queue(), asyncio.Queue()
    approver = WebApprover(outbox, decisions, timeout=0.05)
    assert await approver.prompt(_tool_call()) == "n"


async def test_web_approver_cancel_unblocks() -> None:
    outbox, decisions = asyncio.Queue(), asyncio.Queue()
    approver = WebApprover(outbox, decisions)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_web_approver_drains_stale_decisions() -> None:
    outbox, decisions = asyncio.Queue(), asyncio.Queue()
    approver = WebApprover(outbox, decisions)
    decisions.put_nowait("y")  # stale leftover from a cancelled run
    approver.drain()
    assert decisions.empty()
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    decisions.put_nowait("n")
    assert await task == "n"


# ---- Runtime: streaming runs ---- #


async def test_runtime_run_streams_text_to_outbox(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="hi")]
    )
    rt.start_run("hello")
    frames = await _collect_frames(rt, until="run_done")
    types = [f["type"] for f in frames]
    assert types[0] == "run_started"
    assert "text" in types
    assert types[-1] == "run_done"
    assert frames[-1]["result"]["final_output"] == "hi"
    await rt.shutdown()
    await store.close()


async def test_runtime_tool_call_and_result_flow(make_provider, tmp_path) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="read_file", arguments='{"path": "a.py"}')
            ]
        ),
        LLMResponse(final_text="done"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("content of a.py")

    rt, store = await _make_runtime(tmp_path, make_provider, script, tool_executor=executor)
    rt.start_run("read a file")
    frames = await _collect_frames(rt, until="run_done")
    types = [f["type"] for f in frames]
    assert "tool_call" in types
    assert "tool_result" in types
    tool_result = next(f for f in frames if f["type"] == "tool_result")
    assert tool_result["tool_call_id"] == "t1"
    assert tool_result["name"] == "read_file"
    assert tool_result["is_error"] is False
    assert frames[-1]["type"] == "run_done"
    await rt.shutdown()
    await store.close()


async def test_runtime_max_turns_emits_run_error(make_provider, tmp_path) -> None:
    # One tool-call turn then the budget is exhausted.
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="read_file", arguments='{"path": "a.py"}')
            ]
        )
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("x")

    rt, store = await _make_runtime(
        tmp_path, make_provider, script, tool_executor=executor, HARNESS_MAX_TURNS="1"
    )
    rt.start_run("go")
    frames = await _collect_frames(rt, until="run_error")
    error = frames[-1]
    assert error["type"] == "run_error"
    assert error["error_type"] == "max_turns"
    await rt.shutdown()
    await store.close()


# ---- Runtime: approval / pause / resume / cancel ---- #


async def test_runtime_approval_deny_blocks_tool(make_provider, tmp_path) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="write_file", arguments='{"path": "a", "content": "x"}')
            ]
        ),
        LLMResponse(final_text="ok, will not write"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("should not run")

    rt, store = await _make_runtime(tmp_path, make_provider, script, tool_executor=executor)
    rt.start_run("write a file")
    await _collect_frames(rt, until="approval_required")
    rt.decisions.put_nowait("n")
    frames = await _collect_frames(rt, until="run_done")
    denied = next(f for f in frames if f["type"] == "tool_result")
    assert denied["is_error"] is True
    assert "blocked by user" in denied["content"]
    await rt.shutdown()
    await store.close()


async def test_runtime_pause_then_resume(make_provider, tmp_path) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="write_file", arguments='{"path": "a", "content": "x"}')
            ]
        ),
        LLMResponse(final_text="continued"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("wrote")

    rt, store = await _make_runtime(tmp_path, make_provider, script, tool_executor=executor)
    await rt.start()
    rt.start_run("go")
    await _collect_frames(rt, until="approval_required")
    rt.decisions.put_nowait("p")  # allow + pause after this turn
    paused_frames = await _collect_frames(rt, until="paused")
    paused = paused_frames[-1]
    assert paused["checkpoint_id"]
    assert paused["session_id"] == rt.active_session

    rt.resume()
    resume_frames = await _collect_frames(rt, until="run_done")
    types = [f["type"] for f in resume_frames]
    assert types[0] == "resumed"
    assert "text" in types
    assert types[-1] == "run_done"
    assert resume_frames[-1]["result"]["final_output"] == "continued"
    await rt.shutdown()
    await store.close()


async def test_runtime_cancel_emits_run_cancelled(make_provider, tmp_path) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="write_file", arguments='{"path": "a", "content": "x"}')
            ]
        )
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("wrote")

    rt, store = await _make_runtime(tmp_path, make_provider, script, tool_executor=executor)
    rt.start_run("go")
    await _collect_frames(rt, until="approval_required")
    rt.cancel()
    frames = await _collect_frames(rt, until="run_cancelled")
    assert frames[-1]["type"] == "run_cancelled"
    await rt.shutdown()
    await store.close()


# ---- Runtime: commands ---- #


async def test_runtime_commands_payloads(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(tmp_path, make_provider, [LLMResponse(final_text="x")])

    help_p = await rt.handle_command("help")
    assert "help" in help_p

    tools_p = await rt.handle_command("tools")
    names = [t["name"] for t in tools_p["tools"]]
    assert "read_file" in names and "bash" in names

    skills_p = await rt.handle_command("skills")
    assert skills_p["skills"] == []

    perms_p = await rt.handle_command("permissions")
    assert perms_p["default"] == "ask"

    cps_p = await rt.handle_command("checkpoints")
    assert cps_p["checkpoints"] == []

    old = rt.active_session
    new_p = await rt.handle_command("new")
    assert new_p["session"]["id"] != old
    assert rt.active_session == new_p["session"]["id"]

    clear_p = await rt.handle_command("clear")
    assert clear_p["ok"] is True
    assert clear_p["session_id"] == rt.active_session

    assert await rt.handle_command("nope") is None
    await rt.shutdown()
    await store.close()


# ---- Runtime: plan streaming ---- #


async def test_runtime_plan_streams_plan_events(make_provider, tmp_path) -> None:
    plan_json = (
        '{"goal": "g", "steps": [{"title": "s1", "description": "d1"}]}'
    )
    script = [
        LLMResponse(final_text=plan_json),  # planner.plan
        LLMResponse(final_text="step done"),  # step execution
    ]
    rt, store = await _make_runtime(tmp_path, make_provider, script)
    rt.start_plan("do a thing")
    frames = await _collect_frames(rt, until="plan_done")
    types = [f["type"] for f in frames]
    assert types[0] == "plan_start"
    assert "step_start" in types
    assert "step_end" in types
    assert types[-1] == "plan_done"
    plan_start = next(f for f in frames if f["type"] == "plan_start")
    assert plan_start["plan"]["goal"] == "g"
    step = plan_start["plan"]["steps"][0]
    assert step["title"] == "s1"
    await rt.shutdown()
    await store.close()


# ---- Runtime: session switching ---- #


async def test_runtime_set_session_switches_active(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(tmp_path, make_provider, [LLMResponse(final_text="x")])
    other = await store.sessions.create_session()

    assert await rt.set_session(other.id) is True
    assert rt.active_session == other.id
    assert await rt.set_session("does-not-exist") is False
    assert rt.active_session == other.id
    await rt.shutdown()
    await store.close()
