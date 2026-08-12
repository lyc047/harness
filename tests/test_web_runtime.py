"""Unit tests for the per-connection web runtime (WebApprover + Runtime)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
    named: bool = True,
    **env: str,
) -> tuple[Runtime, Store]:
    settings = _settings(tmp_path, **env)
    store = Store(settings)
    await store.initialize()
    provider = make_provider(script)  # type: ignore[operator]
    rt = build_runtime(settings, store, provider=provider, tool_executor=tool_executor)
    await rt.start()
    # Pre-name the fresh session so the auto-title LLM call can't steal a
    # scripted response in tests that just want to run a message; the dedicated
    # auto-title test opts out with named=False.
    if named and rt.active_session:
        await store.sessions.rename_session(rt.active_session, "test")
    return rt, store


# ---- WebApprover ---- #


def _tool_call() -> ToolCall:
    return ToolCall(id="t1", name="bash", arguments='{"command": "ls"}')


async def test_web_approver_returns_allow_once() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    await approver.approve("t1", "y")
    assert await task == "y"
    frame = json.loads(outbox.get_nowait())
    assert frame["type"] == "approval_required"
    assert frame["tool_call"]["name"] == "bash"


async def test_web_approver_correlates_by_tool_call_id() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    t1 = _tool_call()
    t2 = ToolCall(id="t2", name="bash", arguments='{"command": "ls"}')
    task1 = asyncio.create_task(approver.prompt(t1))
    task2 = asyncio.create_task(approver.prompt(t2))
    await asyncio.sleep(0)
    await approver.approve("t2", "n")
    await approver.approve("t1", "y")
    assert await task2 == "n"
    assert await task1 == "y"  # each decision matched its own call


async def test_web_approver_unknown_id_dropped() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox, timeout=0.05)
    t1 = _tool_call()
    t2 = ToolCall(id="t2", name="bash", arguments='{"command": "ls"}')
    task1 = asyncio.create_task(approver.prompt(t1))
    task2 = asyncio.create_task(approver.prompt(t2))
    await asyncio.sleep(0)
    await approver.approve("nope", "y")  # matches nothing -> dropped
    assert await task1 == "n"  # both time out fail-closed
    assert await task2 == "n"


async def test_web_approver_single_pending_fallback() -> None:
    """Compat bridge: an empty id with exactly one pending approval resolves
    that pending one (old clients that omit the id keep working in sequential
    mode)."""
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    await approver.approve("", "y")
    assert await task == "y"


async def test_web_approver_stale_nonempty_id_does_not_resolve() -> None:
    """A stale non-empty id (from a cancelled run) must NOT resolve a later
    run's sole pending prompt, while the empty-id legacy bridge still does."""
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    t2 = ToolCall(id="t2", name="bash", arguments='{"command": "ls"}')
    task = asyncio.create_task(approver.prompt(t2))
    await asyncio.sleep(0)
    await approver.approve("stale-id", "n")  # non-empty, unknown -> discarded
    assert "t2" in approver._pending
    assert not approver._pending["t2"].done()
    await approver.approve("", "y")  # empty-id bridge still resolves sole pending
    assert await task == "y"


async def test_web_approver_timeout_fails_closed() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox, timeout=0.05)
    assert await approver.prompt(_tool_call()) == "n"


async def test_web_approver_drain_cancels_pending() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    approver.drain()
    with pytest.raises(asyncio.CancelledError):
        await task


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
    await rt.approve("", "n")
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
    await rt.approve("", "p")  # allow + pause after this turn
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
    skill_names = [s["name"] for s in skills_p["skills"]]
    assert "skill-creator" in skill_names  # bundled skill ships with the package

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


# ---- Runtime: auto-title / rollback / branch (three-feature plan) ---- #


def _write(path: str, content: str) -> None:
    """Sync file write so the async tests stay ASYNC240-clean."""
    Path(path).write_text(content, encoding="utf-8")


def _read(path: str) -> str:
    """Sync file read (ASYNC240-clean)."""
    return Path(path).read_text(encoding="utf-8")


async def _auto_approve(rt: Runtime, *, until: str, timeout: float = 5.0) -> list[dict]:
    """Collect frames, feeding "y" for every approval so tool calls proceed."""
    frames: list[dict] = []
    while True:
        raw = await asyncio.wait_for(rt.outbox.get(), timeout=timeout)
        frame = json.loads(raw)
        frames.append(frame)
        if frame["type"] == "approval_required":
            await rt.approve("", "y")
        if frame["type"] == until:
            return frames


async def test_runtime_auto_titles_unnamed_session(make_provider, tmp_path) -> None:
    # The first script response is consumed by the auto-title complete() call.
    script = [
        LLMResponse(final_text="排序任务"),
        LLMResponse(final_text="done"),
    ]
    rt, store = await _make_runtime(tmp_path, make_provider, script, named=False)
    rt.start_run("请帮我给这几个文件排序")
    frames = await _collect_frames(rt, until="session_renamed")
    renamed = frames[-1]
    assert renamed["type"] == "session_renamed"
    assert renamed["session_id"] == rt.active_session
    assert renamed["name"] == "排序任务"
    got = await store.sessions.get_session(rt.active_session)
    assert got is not None and got.name == "排序任务"
    # A named session does not get re-titled.
    rt.start_run("再说一句")
    frames2 = await _collect_frames(rt, until="run_done")
    assert all(f["type"] != "session_renamed" for f in frames2)
    await rt.shutdown()
    await store.close()


async def test_runtime_rollback_restores_files_and_truncates(
    make_provider, tmp_path
) -> None:
    target = tmp_path / "rb.txt"
    _write(str(target), "original")
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": str(target), "content": "v1"}),
                )
            ]
        ),
        LLMResponse(final_text="wrote v1"),
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="w2",
                    name="write_file",
                    arguments=json.dumps({"path": str(target), "content": "v2"}),
                )
            ]
        ),
        LLMResponse(final_text="wrote v2"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        args = tool_call.arguments_dict
        _write(str(args["path"]), str(args.get("content", "")))
        return ToolResult.ok("wrote")

    rt, store = await _make_runtime(tmp_path, make_provider, script, tool_executor=executor)
    # Pre-name the session so the first run doesn't spend a response on auto-title.
    await store.sessions.rename_session(rt.active_session, "rollback-test")

    rt.start_run("先写入 v1")
    await _auto_approve(rt, until="run_done")
    rt.start_run("改成 v2")
    await _auto_approve(rt, until="run_done")
    assert _read(str(target)) == "v2"

    await rt.rollback(1)  # to the first user message — both writes are after it
    frames = await _collect_frames(rt, until="rolled_back")
    rb = frames[-1]
    assert rb["type"] == "rolled_back"
    assert rb["session_id"] == rt.active_session
    assert rb["to_idx"] == 1  # system@0, user@1
    assert str(target) in rb["restored"]

    # Workspace is back to the pre-conversation state; history truncated.
    assert _read(str(target)) == "original"
    loaded = await store.sessions.load_messages(rt.active_session)
    assert [m.role for m in loaded] == ["system", "user"]
    await rt.shutdown()
    await store.close()


async def test_runtime_rollback_invalid_step_emits_error(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(tmp_path, make_provider, [LLMResponse(final_text="x")])
    await rt.rollback(99)
    frames = await _collect_frames(rt, until="run_error")
    assert frames[-1]["error_type"] == "invalid_step"
    await rt.shutdown()
    await store.close()


async def test_runtime_branch_forks_history(make_provider, tmp_path) -> None:
    script = [
        LLMResponse(final_text="a1"),
        LLMResponse(final_text="a2"),
    ]
    rt, store = await _make_runtime(tmp_path, make_provider, script)
    await store.sessions.rename_session(rt.active_session, "parent")
    rt.start_run("q1")
    await _collect_frames(rt, until="run_done")
    rt.start_run("q2")
    await _collect_frames(rt, until="run_done")

    source = rt.active_session
    await rt.branch(2)  # fork after the first Q&A (system@0, user@1, assistant@2)
    frames = await _collect_frames(rt, until="session_switched")
    created = next(f for f in frames if f["type"] == "session_created")
    assert created["session"]["parent_session_id"] == source
    assert created["session"]["name"] == "parent · 分支"
    switched = frames[-1]
    assert switched["session_id"] == created["session"]["id"]
    assert rt.active_session == switched["session_id"]

    forked = await store.sessions.load_messages(switched["session_id"])
    assert [m.content for m in forked if m.role in ("user", "assistant")] == ["q1", "a1"]
    await rt.shutdown()
    await store.close()


# ---- Runtime: subagents ---- #


async def test_runtime_subagents_off_by_default(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(tmp_path, make_provider, [LLMResponse(final_text="x")])
    names = rt.stack.agent.tools.names()
    assert "delegate_to_researcher" not in names
    assert "delegate_to_coder" not in names
    await rt.shutdown()
    await store.close()


async def test_runtime_subagents_enabled_by_setting(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="x")], HARNESS_SUBAGENTS="1"
    )
    names = rt.stack.agent.tools.names()
    assert "delegate_to_researcher" in names
    assert "delegate_to_coder" in names
    await rt.shutdown()
    await store.close()


async def test_runtime_subagent_model_tiering(make_provider, tmp_path) -> None:
    """HARNESS_SUBAGENT_MODEL gives delegates a cheaper model tier; unset, they
    inherit the parent (settings.model)."""
    rt, store = await _make_runtime(
        tmp_path,
        make_provider,
        [LLMResponse(final_text="x")],
        HARNESS_SUBAGENTS="1",
        HARNESS_SUBAGENT_MODEL="cheap-model",
    )
    tool = rt.stack.agent.tools.get("delegate_to_researcher")
    assert tool is not None and tool._model == "cheap-model"
    await rt.shutdown()
    await store.close()

    rt2, store2 = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="x")], HARNESS_SUBAGENTS="1"
    )
    tool2 = rt2.stack.agent.tools.get("delegate_to_researcher")
    assert tool2 is not None and tool2._model == "deepseek-v4-flash"
    await rt2.shutdown()
    await store2.close()


async def test_runtime_subagent_runs_on_cheaper_model(make_provider, tmp_path) -> None:
    """The parent turn hits settings.model; the delegated subagent turn hits
    the configured cheaper tier — same provider, different model per run."""
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="delegate_to_researcher",
                    arguments='{"task": "research something"}',
                )
            ]
        ),
        LLMResponse(final_text="subagent answer"),
        LLMResponse(final_text="parent done"),
    ]
    rt, store = await _make_runtime(
        tmp_path,
        make_provider,
        script,
        HARNESS_SUBAGENTS="1",
        HARNESS_SUBAGENT_MODEL="cheap-model",
    )
    provider = rt.stack.provider
    rt.start_run("research something")
    await _collect_frames(rt, until="approval_required")  # wait for the hand-off prompt
    await rt.approve("", "y")  # approve the hand-off
    await _collect_frames(rt, until="run_done")
    # parent T1 -> subagent turn -> parent T2
    assert provider.models == ["deepseek-v4-flash", "cheap-model", "deepseek-v4-flash"]
    await rt.shutdown()
    await store.close()


async def test_runtime_subagent_tool_runs_isolated_session(
    make_provider, tmp_path
) -> None:
    """A delegate tool call yields a ToolResult and leaves no session behind."""
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="delegate_to_researcher",
                    arguments='{"task": "research something"}',
                )
            ]
        ),
        LLMResponse(final_text="subagent answer"),
        LLMResponse(final_text="parent done"),
    ]
    rt, store = await _make_runtime(
        tmp_path, make_provider, script, HARNESS_SUBAGENTS="1"
    )
    rt.start_run("research something")
    # delegate_to_researcher is ASK policy — wait for, then approve, the hand-off.
    await _collect_frames(rt, until="approval_required")
    await rt.approve("", "y")
    frames = await _collect_frames(rt, until="run_done")
    tool_results = [f for f in frames if f["type"] == "tool_result"]
    tool_results = [f for f in frames if f["type"] == "tool_result"]
    assert tool_results, "expected a delegate tool_result frame"
    assert "subagent answer" in tool_results[0]["content"]
    sessions = await store.sessions.list_sessions(limit=10)
    # The parent run's session only — subagents run with session_id=None.
    assert len(sessions) == 1
    await rt.shutdown()
    await store.close()


async def test_runtime_subagent_events_forwarded(make_provider, tmp_path) -> None:
    """A delegated subagent's own turns stream into the parent's run as nested
    subagent_start/subagent_event/subagent_end frames, so the web can render a
    subagent view (its own tool calls and results) inside the parent bubble."""
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    script = [
        # parent turn 1 -> hand off to the researcher
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="p1",
                    name="delegate_to_researcher",
                    arguments='{"task": "research something"}',
                )
            ]
        ),
        # subagent turn 1 -> reads a file (ALLOW policy, no approval prompt)
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="read_file",
                    arguments=json.dumps({"path": str(target)}),
                )
            ]
        ),
        # subagent turn 2 -> structured delivery
        LLMResponse(final_text="subagent result"),
        # parent turn 2 -> wraps up
        LLMResponse(final_text="parent done"),
    ]
    rt, store = await _make_runtime(
        tmp_path, make_provider, script, HARNESS_SUBAGENTS="1"
    )
    rt.start_run("research something")
    await _collect_frames(rt, until="approval_required")  # wait for the hand-off prompt
    await rt.approve("", "y")  # approve the hand-off only
    frames = await _collect_frames(rt, until="run_done")
    types = [f["type"] for f in frames]
    assert "subagent_start" in types
    assert "subagent_end" in types

    start = next(f for f in frames if f["type"] == "subagent_start")
    assert start["agent"] == "researcher"

    sub_events = [f for f in frames if f["type"] == "subagent_event"]
    assert start["run_id"]
    end = next(f for f in frames if f["type"] == "subagent_end")
    assert end["run_id"] == start["run_id"]
    for ev in sub_events:
        assert ev["run_id"] == start["run_id"]
    ev_types = [f["event"]["type"] for f in sub_events]
    assert "tool_call" in ev_types
    assert "tool_result" in ev_types
    tool_call_ev = next(
        f["event"] for f in sub_events if f["event"]["type"] == "tool_call"
    )
    assert tool_call_ev["tool_call"]["name"] == "read_file"
    tool_result_ev = next(
        f["event"] for f in sub_events if f["event"]["type"] == "tool_result"
    )
    assert "hello" in tool_result_ev["content"]

    end = next(f for f in frames if f["type"] == "subagent_end")
    assert end["agent"] == "researcher"
    assert end["output"] == "subagent result"
    assert end["turns"] == 2
    assert end["is_error"] is False
    await rt.shutdown()
    await store.close()


# ---- Runtime: permission modes ---- #


async def test_runtime_set_mode_emits_mode_changed(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(tmp_path, make_provider, [LLMResponse(final_text="x")])
    assert rt.mode == "ask"  # connection default is manual confirmation
    assert await rt.set_mode("auto") is True
    frames = await _collect_frames(rt, until="mode_changed")
    assert frames[-1]["type"] == "mode_changed"
    assert frames[-1]["mode"] == "auto"
    assert rt.mode == "auto"
    assert await rt.set_mode("bogus") is False
    assert rt.mode == "auto"
    await rt.shutdown()
    await store.close()


async def test_runtime_mcp_add_list_remove(make_provider, tmp_path) -> None:
    fixture = str(Path(__file__).parent / "fixtures" / "mcp_server.py")
    rt, store = await _make_runtime(tmp_path, make_provider, [LLMResponse(final_text="x")])

    added = await rt.handle_command("mcp", f"add stdio demo {fixture}")
    assert added is not None and added["ok"] is True
    assert added["action"] == "added"
    assert {"mcp_demo_add", "mcp_demo_echo", "mcp_demo_fail"} <= set(added["tools"])

    listed = await rt.handle_command("mcp", "list")
    assert listed is not None and listed["ok"] is True
    demo = next(s for s in listed["servers"] if s["name"] == "demo")
    assert len(demo["tools"]) == 3

    removed = await rt.handle_command("mcp", "remove demo")
    assert removed is not None and removed["ok"] is True
    assert removed["action"] == "removed"
    names = rt.stack.agent.tools.names()
    assert not any(n.startswith("mcp_demo_") for n in names)
    await rt.shutdown()
    await store.close()


async def test_runtime_auto_mode_run_skips_approval(make_provider, tmp_path) -> None:
    target = tmp_path / "auto.txt"
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": str(target), "content": "hi"}),
                )
            ]
        ),
        LLMResponse(final_text="wrote"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        args = tool_call.arguments_dict
        _write(str(args["path"]), str(args.get("content", "")))
        return ToolResult.ok("wrote")

    rt, store = await _make_runtime(tmp_path, make_provider, script, tool_executor=executor)
    # write_file is ASK under the default policy — in auto mode it must run
    # without an approval_required frame.
    assert await rt.set_mode("auto") is True
    await _collect_frames(rt, until="mode_changed")  # drain the switch frame

    rt.start_run("write auto.txt")
    frames = await _collect_frames(rt, until="run_done")
    types = [f["type"] for f in frames]
    assert "approval_required" not in types
    assert "tool_result" in types
    assert _read(str(target)) == "hi"
    await rt.shutdown()
    await store.close()


# ---- Runtime: advanced orchestration toggle ---- #


async def test_runtime_set_advanced_roundtrip(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="x")], HARNESS_SUBAGENTS="1"
    )
    assert rt.advanced is False
    assert await rt.set_advanced(True) is True
    frames = await _collect_frames(rt, until="advanced_changed")
    assert frames[-1]["advanced"] is True
    assert rt.advanced is True
    # toggling advanced rebuilds the delegate tool set (unregister + register)
    tool = rt.stack.agent.tools.get("delegate_to_researcher")
    assert tool is not None and len(tool._nested_delegates) >= 1
    # idempotent: toggling again does not duplicate tools (register would raise)
    assert await rt.set_advanced(False) is True
    await _collect_frames(rt, until="advanced_changed")
    tool2 = rt.stack.agent.tools.get("delegate_to_researcher")
    assert tool2 is not None and tool2._nested_delegates == ()
    await rt.shutdown()
    await store.close()


async def test_runtime_start_run_resets_budget(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="x")], HARNESS_SUBAGENTS="1"
    )
    rt.stack.subagent_budget.record(20)
    assert rt.stack.subagent_budget.remaining() == 20  # 40 - 20
    rt.start_run("go")
    frames = await _collect_frames(rt, until="run_done")
    assert frames[-1]["type"] == "run_done"
    assert rt.stack.subagent_budget.remaining() == 40  # reset at run start
    await rt.shutdown()
    await store.close()
