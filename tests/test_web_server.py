"""End-to-end tests for the FastAPI web server (REST + WebSocket).

Uses ``fastapi.testclient.TestClient`` with an injected scripted provider and
tool executor — no real model calls, no subprocesses (Windows-safe). Each test
builds its own app/store in a tmp dir so tests never share sessions.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from harness.config import Settings
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.llm.base import LLMResponse
from harness.memory.store import Store
from harness.tools.base import ToolResult
from harness.web.runtime import Runtime, build_runtime
from harness.web.server import create_app


def _settings(tmp_path: object, **env: str) -> Settings:
    base = {
        "HARNESS_DB_PATH": str(tmp_path / "harness.db"),  # type: ignore[operator]
        "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),  # type: ignore[operator]
        "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),  # type: ignore[operator]
    }
    base.update(env)
    return Settings.from_env(base)


def _make_factory(
    make_provider: object,
    script: list[LLMResponse] | None = None,
    tool_executor: ToolExecutor | None = None,
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
        return build_runtime(
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
    **env: str,
) -> TestClient:
    settings = _settings(tmp_path, **env)
    store = Store(settings)
    app = create_app(
        settings,
        store=store,
        build_runtime_factory=_make_factory(make_provider, script, tool_executor),  # type: ignore[arg-type]
    )
    return TestClient(app)


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
