"""End-to-end tests for the FastAPI web server (REST + WebSocket).

Uses ``fastapi.testclient.TestClient`` with an injected scripted provider and
tool executor — no real model calls, no subprocesses (Windows-safe). Each test
builds its own app/store in a tmp dir so tests never share sessions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from harness.config import Settings
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.llm.base import (
    LLMResponse,
    StreamEnd,
    StreamReasoning,
    StreamText,
    StreamToolCall,
)
from harness.memory.store import Store
from harness.tools.base import ToolResult
from harness.web.runtime import Runtime
from harness.web.server import create_app


def _settings(tmp_path: object, **env: str) -> Settings:
    base = {
        "HARNESS_DB_PATH": str(tmp_path / "harness.db"),  # type: ignore[operator]
        "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),  # type: ignore[operator]
        "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),  # type: ignore[operator]
    }
    base.update(env)
    return Settings.from_env(base)


class _NamedRuntime(Runtime):
    """Runtime that pre-names its fresh session on start.

    The auto-title LLM call would otherwise consume a test's first scripted
    response before the run ever starts. Message-sending tests want a named
    session; the auto-title/rollback/branch round-trip opts out with
    ``pre_name=False``.
    """

    async def start(self) -> None:
        await super().start()
        if self.active_session is not None:
            await self._store.sessions.rename_session(self.active_session, "test")


def _make_factory(
    make_provider: object,
    script: list[LLMResponse] | None = None,
    tool_executor: ToolExecutor | None = None,
    *,
    pre_name: bool = True,
) -> object:
    """Build the ``build_runtime_factory`` seam create_app takes.

    All WS connections in one test share a scripted FakeProvider; each test
    uses a single connection so script consumption stays deterministic.
    """
    provider = make_provider(script)  # type: ignore[operator]

    def factory(
        settings: Settings,
        store: Store,
        *,
        active_session: str | None = None,
        **kwargs: object,
    ) -> Runtime:
        cls = _NamedRuntime if pre_name else Runtime
        return cls(
            settings,
            store,
            provider=provider,
            tool_executor=tool_executor,
            active_session=active_session,
            **kwargs,
        )

    return factory


def _client(
    tmp_path: object,
    make_provider: object,
    script: list[LLMResponse] | None = None,
    tool_executor: ToolExecutor | None = None,
    *,
    pre_name: bool = True,
    **env: str,
) -> TestClient:
    settings = _settings(tmp_path, **env)
    store = Store(settings)
    app = create_app(
        settings,
        store=store,
        build_runtime_factory=_make_factory(  # type: ignore[arg-type]
            make_provider, script, tool_executor, pre_name=pre_name
        ),
    )
    return TestClient(app)


def _write(path: str, content: str) -> None:
    """Sync file write (ASYNC240-clean inside async test executors)."""
    Path(path).write_text(content, encoding="utf-8")


def _recv_until(ws: object, until_type: str, timeout: float = 10.0) -> list[dict]:
    """Receive WS frames until a frame of ``until_type`` arrives (or timeout)."""
    frames: list[dict] = []
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() > deadline:
            types = [f.get("type") for f in frames]
            raise AssertionError(f"timed out waiting for {until_type!r}; got {types}")
        frame = ws.receive_json()  # type: ignore[attr-defined]
        frames.append(frame)
        if frame.get("type") == until_type:
            return frames


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------


def test_rest_health_index_and_static(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        index = client.get("/")
        assert index.status_code == 200
        assert "harness" in index.text and "approval-overlay" in index.text

        css = client.get("/static/style.css")
        assert css.status_code == 200
        js = client.get("/static/js/app.js")
        assert js.status_code == 200


def test_rest_sessions_crud(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        empty = client.get("/api/sessions").json()
        assert empty["sessions"] == []

        created = client.post("/api/sessions")
        assert created.status_code == 201
        sid = created.json()["session"]["id"]

        listed = client.get("/api/sessions").json()
        assert [s["id"] for s in listed["sessions"]] == [sid]

        messages = client.get(f"/api/sessions/{sid}/messages")
        assert messages.status_code == 200
        assert messages.json()["messages"] == []

        assert client.get("/api/sessions/nope/messages").status_code == 404

        deleted = client.delete(f"/api/sessions/{sid}")
        assert deleted.status_code == 200
        assert client.get("/api/sessions").json()["sessions"] == []


def test_rest_session_payload_includes_name_and_patch_rename(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        created = client.post("/api/sessions").json()["session"]
        sid = created["id"]
        assert created["name"] is None
        assert created["parent_session_id"] is None

        # PATCH rename → list reflects the new name
        patched = client.patch(f"/api/sessions/{sid}", json={"name": "我的标题"})
        assert patched.status_code == 200
        listed = client.get("/api/sessions").json()["sessions"]
        assert listed[0]["name"] == "我的标题"
        assert listed[0]["id"] == sid

        # unknown session / blank name → 404 / 422
        assert client.patch("/api/sessions/nope", json={"name": "x"}).status_code == 404
        assert client.patch(f"/api/sessions/{sid}", json={"name": "  "}).status_code == 422


def test_rest_tools_skills_permissions_help(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        tools = client.get("/api/tools").json()
        names = [t["name"] for t in tools["tools"]]
        assert "read_file" in names and "bash" in names

        skills = client.get("/api/skills").json()
        assert "skills" in skills

        perms = client.get("/api/permissions").json()
        assert perms["default"] == "ask"

        help_payload = client.get("/api/help").json()
        assert "/plan" in help_payload["help"]

        checkpoints = client.get("/api/checkpoints").json()
        assert checkpoints["checkpoints"] == []


def test_rest_tools_reflect_subagents_setting(tmp_path, make_provider) -> None:
    # Default: no delegate tools.
    with _client(tmp_path, make_provider) as client:
        names = [t["name"] for t in client.get("/api/tools").json()["tools"]]
        assert "delegate_to_researcher" not in names
        assert "delegate_to_coder" not in names

    # HARNESS_SUBAGENTS=1: the REST tools view shows the delegation tools too.
    with _client(tmp_path, make_provider, HARNESS_SUBAGENTS="1") as client:
        names = [t["name"] for t in client.get("/api/tools").json()["tools"]]
        assert "delegate_to_researcher" in names
        assert "delegate_to_coder" in names


# --------------------------------------------------------------------------
# WebSocket: ready + streaming
# --------------------------------------------------------------------------


def test_ws_ready_then_text_stream(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider, [LLMResponse(final_text="hi there")]) as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["session_id"]
            assert ready["model"]

            ws.send_json({"type": "message", "content": "hello"})
            frames = _recv_until(ws, "run_done")
            types = [f["type"] for f in frames]
            assert types[0] == "run_started"
            assert "text" in types
            assert types[-1] == "run_done"
            assert frames[-1]["result"]["final_output"] == "hi there"


def test_ws_tool_flow_with_approval_allow(tmp_path, make_provider) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="write_file", arguments='{"path": "a.py", "content": "x"}')
            ]
        ),
        LLMResponse(final_text="wrote it"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("file written")

    with _client(tmp_path, make_provider, script, executor) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "write a.py"})
            frames = _recv_until(ws, "approval_required")
            approval = frames[-1]
            assert approval["tool_call"]["name"] == "write_file"

            ws.send_json({"type": "approval", "decision": "y"})
            frames = _recv_until(ws, "run_done")
            types = [f["type"] for f in frames]
            assert "tool_result" in types
            result = next(f for f in frames if f["type"] == "tool_result")
            assert result["tool_call_id"] == "t1"
            assert result["content"] == "file written"
            assert result["is_error"] is False


def test_ws_approval_deny_blocks_tool(tmp_path, make_provider) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="write_file", arguments='{"path": "a", "content": "x"}')
            ]
        ),
        LLMResponse(final_text="ok, denied"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("should not run")

    with _client(tmp_path, make_provider, script, executor) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "write a file"})
            _recv_until(ws, "approval_required")
            ws.send_json({"type": "approval", "decision": "n"})
            frames = _recv_until(ws, "run_done")
            result = next(f for f in frames if f["type"] == "tool_result")
            assert result["is_error"] is True
            assert "blocked by user" in result["content"]


def test_ws_approval_edit_args(tmp_path, make_provider) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="write_file",
                    arguments='{"path": "orig.py", "content": "x"}',
                )
            ]
        ),
        LLMResponse(final_text="edited"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok(f"ran with {tool_call.arguments}")

    with _client(tmp_path, make_provider, script, executor) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "write a file"})
            _recv_until(ws, "approval_required")
            ws.send_json(
                {"type": "approval", "decision": 'e:{"path": "edited.py", "content": "x"}'}
            )
            frames = _recv_until(ws, "run_done")
            result = next(f for f in frames if f["type"] == "tool_result")
            assert '"edited.py"' in result["content"]
            assert "orig.py" not in result["content"]


def test_ws_pause_then_resume(tmp_path, make_provider) -> None:
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

    with _client(tmp_path, make_provider, script, executor) as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            ws.send_json({"type": "message", "content": "go"})
            _recv_until(ws, "approval_required")
            ws.send_json({"type": "approval", "decision": "p"})  # allow + pause
            paused = _recv_until(ws, "paused")[-1]
            assert paused["checkpoint_id"]
            assert paused["session_id"] == ready["session_id"]

            ws.send_json({"type": "resume"})
            frames = _recv_until(ws, "run_done")
            types = [f["type"] for f in frames]
            assert types[0] == "resumed"
            assert frames[-1]["result"]["final_output"] == "continued"


def test_ws_cancel_emits_run_cancelled(tmp_path, make_provider) -> None:
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="t1", name="write_file", arguments='{"path": "a", "content": "x"}')
            ]
        ),
        LLMResponse(final_text="done"),
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        return ToolResult.ok("wrote")

    with _client(tmp_path, make_provider, script, executor) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "go"})
            _recv_until(ws, "approval_required")
            ws.send_json({"type": "cancel"})
            frames = _recv_until(ws, "run_cancelled")
            assert frames[-1]["type"] == "run_cancelled"


# --------------------------------------------------------------------------
# WebSocket: commands / sessions / reconnect
# --------------------------------------------------------------------------


def test_ws_command_tools_payload(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "command", "name": "tools"})
            frame = _recv_until(ws, "command_result")[-1]
            assert frame["name"] == "tools"
            names = [t["name"] for t in frame["payload"]["tools"]]
            assert "read_file" in names and "bash" in names


def test_ws_new_command_creates_session(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            ws.send_json({"type": "command", "name": "new"})
            frame = _recv_until(ws, "session_created")[-1]
            new_sid = frame["session"]["id"]
            assert new_sid != ready["session_id"]
            # the command result carries the same payload
            result = _recv_until(ws, "command_result")[-1]
            assert result["payload"]["session"]["id"] == new_sid


def test_ws_set_session_switches(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()  # rt.start() created the initial session
            other = client.post("/api/sessions").json()["session"]["id"]
            assert ready["session_id"] != other

            ws.send_json({"type": "set_session", "session_id": other})
            frame = _recv_until(ws, "session_switched")[-1]
            assert frame["session_id"] == other

            # unknown session -> session_error
            ws.send_json({"type": "set_session", "session_id": "does-not-exist"})
            err = _recv_until(ws, "session_error")[-1]
            assert err["session_id"] == "does-not-exist"


def test_ws_disconnect_then_reconnect(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            sid = ready["session_id"]

        # session survives the disconnect; a new connection can resume it
        with client.websocket_connect(f"/ws?session_id={sid}") as ws2:
            ready2 = ws2.receive_json()
            assert ready2["type"] == "ready"
            assert ready2["session_id"] == sid

            # and it can still run
            ws2.send_json({"type": "message", "content": "hi again"})
            frames = _recv_until(ws2, "run_done")
            assert frames[-1]["result"]["final_output"] == "(no script)"


def test_ws_rollback_and_branch_roundtrip(tmp_path, make_provider) -> None:
    target = tmp_path / "rb.txt"
    target.write_text("original", encoding="utf-8")

    # script[0] is consumed by the auto-title complete() on the first message.
    script = [
        LLMResponse(final_text="排序"),
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
    ]

    async def executor(agent, tool_call):  # noqa: ARG001
        args = tool_call.arguments_dict
        _write(str(args["path"]), str(args.get("content", "")))
        return ToolResult.ok("wrote")

    with _client(tmp_path, make_provider, script, executor, pre_name=False) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "先写入 v1"})
            frames: list[dict] = []
            while True:
                frame = ws.receive_json()
                frames.append(frame)
                if frame.get("type") == "approval_required":
                    ws.send_json({"type": "approval", "decision": "y"})
                if frame.get("type") == "run_done":
                    break
            assert any(f["type"] == "session_renamed" for f in frames)
            assert target.read_text(encoding="utf-8") == "v1"

            # rollback to step 1 → file back to original, history truncated
            ws.send_json({"type": "rollback", "step": 1})
            rb = _recv_until(ws, "rolled_back")[-1]
            assert rb["session_id"]
            assert str(target) in rb["restored"]
            assert target.read_text(encoding="utf-8") == "original"

            # branch from the same point → new child session
            ws.send_json({"type": "branch", "step": 1})
            branch_frames = _recv_until(ws, "session_switched")
            created = next(f for f in branch_frames if f["type"] == "session_created")
            assert created["session"]["parent_session_id"] == rb["session_id"]
            switched = branch_frames[-1]
            assert switched["session_id"] == created["session"]["id"]
            assert "分支" in created["session"]["name"]


# --------------------------------------------------------------------------
# WebSocket: permission modes
# --------------------------------------------------------------------------


def test_ws_ready_reports_mode_and_set_mode_roundtrip(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider) as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["mode"] == "ask"  # connection default is manual confirmation

            ws.send_json({"type": "set_mode", "mode": "auto"})
            assert _recv_until(ws, "mode_changed")[-1]["mode"] == "auto"

            ws.send_json({"type": "set_mode", "mode": "full"})
            assert _recv_until(ws, "mode_changed")[-1]["mode"] == "full"

            ws.send_json({"type": "set_mode", "mode": "bogus"})
            err = _recv_until(ws, "mode_error")[-1]
            assert err["type"] == "mode_error"
            assert "bogus" in err["message"]


# --------------------------------------------------------------------------
# WebSocket: MCP commands
# --------------------------------------------------------------------------


def test_ws_mcp_command_roundtrip(tmp_path, make_provider) -> None:
    fixture = str(Path(__file__).parent / "fixtures" / "mcp_server.py")
    with _client(tmp_path, make_provider) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json(
                {"type": "command", "name": "mcp", "arg": f"add stdio demo {fixture}"}
            )
            result = _recv_until(ws, "command_result")[-1]
            assert result["name"] == "mcp" and result["ok"] is True
            assert result["payload"]["action"] == "added"
            assert "mcp_demo_add" in result["payload"]["tools"]

            ws.send_json({"type": "command", "name": "mcp", "arg": "list"})
            listed = _recv_until(ws, "command_result")[-1]
            demo = next(s for s in listed["payload"]["servers"] if s["name"] == "demo")
            assert len(demo["tools"]) == 3

            ws.send_json({"type": "command", "name": "mcp", "arg": "remove demo"})
            result = _recv_until(ws, "command_result")[-1]
            assert result["ok"] is True
            assert result["payload"]["action"] == "removed"


# --------------------------------------------------------------------------
# WebSocket: streaming frames a reasoning model produces
# --------------------------------------------------------------------------


class _ReasoningProvider:
    """Scripted provider that streams thinking like deepseek-v4-flash: a
    reasoning model emits reasoning_content + tool calls during work turns and
    reasoning_content + content on the final turn — never content between tools.
    Locks the frame contract the frontend's thinking panel and final-reply
    fallback depend on."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = script

    async def complete(
        self, messages: object, *, tools: object = None, model: object = None
    ) -> LLMResponse:
        events = [e async for e in self.stream(messages, tools=tools, model=model)]  # type: ignore[arg-type]
        return next(e.response for e in events if isinstance(e, StreamEnd))

    async def stream(self, messages: object, *, tools: object = None, model: object = None):  # type: ignore[no-untyped-def]
        response = (
            self._script.pop(0) if self._script else LLMResponse(final_text="(no script)")
        )
        if response.reasoning_content:
            yield StreamReasoning(text=response.reasoning_content)
        if response.tool_calls:
            for tc in response.tool_calls:
                yield StreamToolCall(tool_call=tc)
        if response.final_text:
            yield StreamText(text=response.final_text)
        yield StreamEnd(response=response)


def test_ws_streams_reasoning_then_tools_then_final(tmp_path) -> None:
    """A reasoning-model run reaches the client as thinking + tools + a final
    answer, and ``run_done`` always carries ``final_output`` — the field the
    frontend falls back to when the final turn streams no visible text."""
    script = [
        LLMResponse(
            reasoning_content="need to search",
            tool_calls=[
                ToolCall(id="t1", name="grep_files", arguments='{"pattern": "x"}')
            ],
        ),
        LLMResponse(reasoning_content="found it", final_text="found it"),
    ]

    async def executor(agent: object, tool_call: ToolCall) -> ToolResult:
        return ToolResult.ok("ok")

    with _client(tmp_path, lambda s: _ReasoningProvider(s), script, executor) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "search x"})
            frames = _recv_until(ws, "run_done")
            types = [f["type"] for f in frames]
            assert "reasoning" in types
            assert "tool_call" in types
            assert "tool_result" in types
            assert "text" in types
            assert types[-1] == "run_done"
            assert frames[-1]["result"]["final_output"] == "found it"


def test_ws_run_done_with_empty_final_keeps_contract(tmp_path) -> None:
    """A run whose final turn streams only thinking (no content) still emits
    ``run_done`` with ``final_output`` — the boundary the frontend's
    final-reply fallback handles (empty output → the visible thinking panel)."""
    script = [LLMResponse(reasoning_content="the answer is 5")]
    with _client(tmp_path, lambda s: _ReasoningProvider(s), script) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "content": "hi"})
            frames = _recv_until(ws, "run_done")
            assert frames[-1]["type"] == "run_done"
            assert frames[-1]["result"]["final_output"] is None
            assert any(f["type"] == "reasoning" for f in frames)
