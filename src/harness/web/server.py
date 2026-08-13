"""FastAPI app for the Codex-style web agent interface.

REST handles instant operations (sessions, history, tools/skills/permissions,
checkpoints, help); the WebSocket carries streaming runs, approval decisions,
pause/resume, plan execution and the two session-mutating commands (``/new``,
``/clear``). Both share :mod:`harness.web.commands`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from harness.config import Settings
from harness.core.compose import add_example_subagents, build_core_stack
from harness.memory.store import Store
from harness.web import commands
from harness.web.events import serialize_messages
from harness.web.runtime import Runtime, build_runtime

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    build_runtime_factory: Callable[..., Runtime] = build_runtime,
) -> FastAPI:
    """Build the FastAPI application.

    ``settings`` / ``store`` are injectable for tests; otherwise the lifespan
    loads settings (``Settings.load()``) and creates+initializes the single
    shared store. ``build_runtime_factory`` is the seam tests use to inject a
    scripted provider / tool executor into each connection's runtime.
    """
    resolved_settings = settings

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> Any:
        s = resolved_settings if resolved_settings is not None else Settings.load()
        st = store if store is not None else Store(s)
        await st.initialize()
        # One read-only stack shared by the stateless REST reads.
        read_ctx = await build_core_stack(s, store=st, prompt=None)
        if s.subagents:
            add_example_subagents(read_ctx, subagent_fallback_model=s.subagent_fallback_model)
        app.state.settings = s
        app.state.store = st
        app.state.read_ctx = read_ctx
        yield
        await st.close()

    app = FastAPI(title="harness web", lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- REST: instant operations ---- #

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "model": app.state.settings.model}

    @app.get("/api/sessions")
    async def list_sessions(limit: int = 50) -> dict[str, Any]:
        sessions = await app.state.store.sessions.list_sessions(limit=limit)
        return {
            "sessions": [commands.session_dict(s) for s in sessions]
        }

    @app.post("/api/sessions")
    async def create_session() -> JSONResponse:
        payload = await commands.new_session_payload(app.state.store)
        return JSONResponse(payload, status_code=201)

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Rename a session (custom naming; the sidebar double-click editor)."""
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        if not await app.state.store.sessions.rename_session(session_id, name):
            raise HTTPException(status_code=404, detail="unknown session")
        return {"ok": True, "session_id": session_id, "name": name}

    @app.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str) -> dict[str, Any]:
        if await app.state.store.sessions.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        messages = await app.state.store.sessions.load_messages(session_id)
        return {"session_id": session_id, "messages": serialize_messages(messages)}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        await app.state.store.sessions.delete_session(session_id)
        return {"ok": True}

    @app.get("/api/checkpoints")
    async def list_checkpoints(limit: int = 10) -> dict[str, Any]:
        return await commands.checkpoints_payload(app.state.store)

    @app.delete("/api/checkpoints/{checkpoint_id}")
    async def delete_checkpoint(checkpoint_id: str) -> dict[str, Any]:
        await app.state.store.sessions.delete_checkpoint(checkpoint_id)
        return {"ok": True}

    @app.get("/api/tools")
    async def tools() -> dict[str, Any]:
        return commands.tools_payload(app.state.read_ctx.agent)

    @app.get("/api/skills")
    async def skills() -> dict[str, Any]:
        return commands.skills_payload(app.state.read_ctx.skill_registry)

    @app.get("/api/permissions")
    async def permissions() -> dict[str, Any]:
        return commands.permissions_payload(app.state.read_ctx.permissions)

    @app.get("/api/help")
    async def help_endpoint() -> dict[str, Any]:
        return commands.help_payload()

    # ---- WebSocket: streaming runtime ---- #

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        settings = app.state.settings
        store = app.state.store
        rt = build_runtime_factory(
            settings, store, active_session=websocket.query_params.get("session_id")
        )
        try:
            await rt.start()
        except Exception as exc:  # noqa: BLE001 — a broken stack must not crash the server
            await websocket.send_json({"type": "fatal", "message": f"{type(exc).__name__}: {exc}"})
            await websocket.close()
            return

        send_task = asyncio.create_task(_sender(websocket, rt))
        try:
            await rt._emit(  # noqa: SLF001 — same-package internal protocol
                {
                    "type": "ready",
                    "session_id": rt.active_session,
                    "model": settings.model,
                    "sandbox_mode": settings.sandbox_mode,
                    "permissions_default": rt.stack.permissions.default.value,
                    "mode": rt.mode,
                    "advanced": rt.advanced,
                    "subagents": settings.subagents,
                    "max_turns": settings.max_turns,
                }
            )
            while True:
                msg = await websocket.receive_json()
                await _dispatch(websocket, rt, msg)
        except WebSocketDisconnect:
            pass
        finally:
            rt.cancel()
            await rt.shutdown()
            send_task.cancel()

    return app


async def _sender(websocket: WebSocket, rt: Runtime) -> None:
    """The single writer of WS frames, draining the runtime's outbox."""
    while True:
        raw = await rt.outbox.get()
        try:
            await websocket.send_text(raw)
        except Exception:  # noqa: BLE001 — socket closed mid-flight
            return


async def _dispatch(websocket: WebSocket, rt: Runtime, msg: dict[str, Any]) -> None:
    """Route one client message to the runtime. Awaiting inline keeps the
    order-sensitive messages (set_session / message) serialized."""
    mtype = msg.get("type")
    if mtype == "set_session":
        session_id = str(msg.get("session_id", ""))
        if await rt.set_session(session_id):
            await rt._emit({"type": "session_switched", "session_id": session_id})  # noqa: SLF001
        else:
            await rt._emit(  # noqa: SLF001
                {"type": "session_error", "session_id": session_id, "message": "no such session"}
            )
    elif mtype == "message":
        content = str(msg.get("content", ""))
        if content.strip():
            rt.start_run(content)
    elif mtype == "plan":
        goal = str(msg.get("goal", ""))
        if goal.strip():
            rt.start_plan(goal)
    elif mtype == "approval":
        await rt.approve(
            str(msg.get("tool_call_id", "")),
            str(msg.get("decision", "n")),
        )
    elif mtype == "pause":
        rt.request_pause()
    elif mtype == "resume":
        rt.resume()
    elif mtype == "resume_checkpoint":
        rt.resume_checkpoint(str(msg.get("checkpoint_id", "")))
    elif mtype == "rollback":
        try:
            step = int(msg.get("step", 0))
        except (TypeError, ValueError):
            step = 0
        await rt.rollback(step)
    elif mtype == "branch":
        try:
            step = int(msg.get("step", 0))
        except (TypeError, ValueError):
            step = 0
        await rt.branch(step)
    elif mtype == "cancel":
        rt.cancel()
    elif mtype == "set_mode":
        mode = str(msg.get("mode", ""))
        if not await rt.set_mode(mode):
            await rt._emit(  # noqa: SLF001
                {"type": "mode_error", "message": f"unknown mode: {mode}"}
            )
    elif mtype == "set_advanced":
        await rt.set_advanced(bool(msg.get("advanced", False)))
    elif mtype == "command":
        name = str(msg.get("name", ""))
        arg = str(msg.get("arg", ""))
        payload = await rt.handle_command(name, arg)
        await rt._emit(  # noqa: SLF001
            {
                "type": "command_result",
                "name": name,
                "ok": payload is not None,
                "payload": payload,
            }
        )
    elif mtype == "ping":
        await rt._emit({"type": "pong"})  # noqa: SLF001
