"""Per-connection runtime for the web UI.

Each WebSocket connection owns a :class:`Runtime` — the stateful, single-user
pieces (agent, runner, approval, planner) that mirror the CLI's composition —
plus the async tasks and queues that connect the run loop to one client. The
server process itself owns the single shared :class:`Store`, so N tabs share
one SQLite connection set (WAL + busy_timeout handle concurrent writers).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from harness.config import Settings
from harness.core.compose import CoreStack, add_example_subagents, build_core_stack
from harness.core.messages import ToolCall
from harness.core.run_result import MaxTurnsExceeded, RunPaused, RunState
from harness.core.runner import ToolExecutor
from harness.llm.base import LLMProvider
from harness.memory.store import Store
from harness.observability.logging import get_logger
from harness.planning.executor import PlanExecutor
from harness.planning.models import Plan
from harness.web import commands
from harness.web.events import plan_to_dict, serialize_event

logger = get_logger("web.runtime")

APPROVAL_TIMEOUT = 300.0


class WebApprover:
    """Approval prompt that pushes a request to the outbox and awaits a decision.

    ``prompt`` runs inside the run task (called by ``ApprovalExecutor``). It
    emits an ``approval_required`` frame to the outbox, then blocks on the
    ``decisions`` queue until the WS receive loop puts the user's choice there.
    A timeout fails closed (``"n"``); a cancelled run propagates
    ``CancelledError`` so the run task can shut down cleanly.
    """

    def __init__(
        self,
        outbox: asyncio.Queue[str],
        decisions: asyncio.Queue[str],
        *,
        timeout: float = APPROVAL_TIMEOUT,
    ) -> None:
        self._outbox = outbox
        self._decisions = decisions
        self._timeout = timeout

    async def prompt(self, tool_call: ToolCall) -> str:
        payload = {
            "type": "approval_required",
            "tool_call": {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        }
        await self._outbox.put(json.dumps(payload, ensure_ascii=False))
        try:
            return await asyncio.wait_for(self._decisions.get(), timeout=self._timeout)
        except TimeoutError:
            logger.warning("approval for %r timed out; fail closed", tool_call.name)
            return "n"

    def drain(self) -> None:
        """Discard stale decisions so a cancelled run's leftovers can't satisfy
        the next prompt."""
        while True:
            try:
                self._decisions.get_nowait()
            except asyncio.QueueEmpty:
                return


class Runtime:
    """Stateful runtime bound to a single WebSocket connection."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        provider: LLMProvider | None = None,
        tool_executor: ToolExecutor | None = None,
        approval_timeout: float = APPROVAL_TIMEOUT,
        active_session: str | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self.outbox: asyncio.Queue[str] = asyncio.Queue()
        self.decisions: asyncio.Queue[str] = asyncio.Queue()
        self._approver = WebApprover(self.outbox, self.decisions, timeout=approval_timeout)
        self._provider = provider
        self._tool_executor = tool_executor
        self._active_session = active_session
        self._stack: CoreStack | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._pause_requested = False
        self._last_state: RunState | None = None
        self._last_checkpoint_id: str | None = None
        self._current_plan: Plan | None = None

    # -- lifecycle -- #

    async def start(self) -> None:
        """Build the core stack (mirrors the CLI composition) and pick a session."""
        self._stack = await build_core_stack(
            self._settings,
            store=self._store,
            provider=self._provider,
            tool_executor=self._tool_executor,
            prompt=self._approver.prompt,
            on_pause=self._on_pause,
            pause_check=self._pause_check,
        )
        if self._settings.subagents:
            self._enable_subagents()
        if self._active_session is None:
            sessions = await self._store.sessions.list_sessions(limit=1)
            if sessions:
                self._active_session = sessions[0].id
            else:
                self._active_session = (await self._store.sessions.create_session()).id

    def _enable_subagents(self) -> None:
        """Register the researcher/coder delegate tools on the agent.

        Delegation tools land under the default ASK policy, so the user approves
        each hand-off (and the subagent's own tool calls) in the web dialog.
        """
        add_example_subagents(self.stack)

    @property
    def stack(self) -> CoreStack:
        if self._stack is None:
            raise RuntimeError("Runtime not started; call start() first")
        return self._stack

    @property
    def active_session(self) -> str | None:
        return self._active_session

    @property
    def current_plan(self) -> Plan | None:
        return self._current_plan

    async def shutdown(self) -> None:
        """Cancel the run task and wait briefly for it to observe the cancel.

        Does NOT close the shared store — the server lifespan owns that.
        """
        self.cancel()
        if self._run_task is not None:
            try:
                await asyncio.wait_for(self._run_task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    # -- session handling -- #

    async def set_session(self, session_id: str) -> bool:
        """Switch the active session; False if the id is unknown."""
        existing = await self._store.sessions.get_session(session_id)
        if existing is None:
            return False
        self._active_session = session_id
        return True

    # -- public controls -- #

    def start_run(self, content: str) -> None:
        """Cancel any active run and start a new one with the given message."""
        self._cancel_current()
        self._approver.drain()
        self._pause_requested = False
        self._run_task = asyncio.create_task(self._run_task_coro(content))

    def start_plan(self, goal: str) -> None:
        """Cancel any active run and start plan generation + execution."""
        self._cancel_current()
        self._approver.drain()
        self._pause_requested = False
        self._run_task = asyncio.create_task(self._plan_task_coro(goal))

    def request_pause(self) -> None:
        """Request a pause; checked at the next turn boundary."""
        self._pause_requested = True

    def cancel(self) -> None:
        """Cancel the active run task (if any)."""
        self._cancel_current()

    def resume(self) -> None:
        """Resume the connection's last paused checkpoint."""
        if self._last_state is not None:
            self._cancel_current()
            self._approver.drain()
            self._pause_requested = False
            self._run_task = asyncio.create_task(self._resume_task_coro(self._last_state))

    def resume_checkpoint(self, checkpoint_id: str) -> None:
        """Resume an arbitrary stored checkpoint by id."""
        self._cancel_current()
        self._approver.drain()
        self._pause_requested = False
        self._run_task = asyncio.create_task(self._resume_checkpoint_coro(checkpoint_id))

    async def handle_command(self, name: str, arg: str = "") -> dict[str, Any] | None:
        """Run a slash command, returning its structured payload (None if unknown)."""
        stack = self.stack
        if name == "help":
            return commands.help_payload()
        if name == "tools":
            return commands.tools_payload(stack.agent)
        if name == "skills":
            return commands.skills_payload(stack.skill_registry)
        if name == "permissions":
            return commands.permissions_payload(stack.permissions)
        if name == "checkpoints":
            return await commands.checkpoints_payload(self._store)
        if name == "new":
            payload = await commands.new_session_payload(self._store)
            session_id = str(payload["session"]["id"])
            self._active_session = session_id
            await self._emit({"type": "session_created", "session": payload["session"]})
            return payload
        if name == "clear":
            return await commands.clear_payload(
                self._store, self._active_session, stack.agent
            )
        return None

    # -- pause wiring -- #

    def _on_pause(self) -> None:
        self._pause_requested = True

    def _pause_check(self, _state: RunState) -> bool:
        return self._pause_requested

    # -- internals -- #

    async def _emit(self, payload: dict[str, Any]) -> None:
        await self.outbox.put(json.dumps(payload, ensure_ascii=False))

    def _cancel_current(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        self._run_task = None

    async def _run_task_coro(self, content: str) -> None:
        stack = self.stack
        await self._emit({"type": "run_started", "session_id": self._active_session})
        try:
            async for event in stack.runner.run_streamed(
                stack.agent, content, session_id=self._active_session
            ):
                frame = serialize_event(event)
                if frame is not None:
                    await self._emit(frame)
        except RunPaused as exc:
            await self._handle_pause(exc.state)
        except asyncio.CancelledError:
            await self._emit({"type": "run_cancelled"})
            raise
        except MaxTurnsExceeded:
            await self._emit(
                {"type": "run_error", "message": "max turns exceeded", "error_type": "max_turns"}
            )
        except Exception as exc:  # noqa: BLE001 — surface any run failure to the client
            logger.exception("run failed")
            await self._emit(
                {
                    "type": "run_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            if asyncio.current_task() is self._run_task:
                self._run_task = None

    async def _plan_task_coro(self, goal: str) -> None:
        stack = self.stack
        planner = stack.planner
        try:
            plan = await planner.plan(goal)
        except Exception as exc:  # noqa: BLE001 — planning failures are visible to the client
            await self._emit(
                {
                    "type": "run_error",
                    "message": f"planning failed: {type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                }
            )
            if asyncio.current_task() is self._run_task:
                self._run_task = None
            return
        self._current_plan = plan
        await self._emit({"type": "plan_start", "plan": plan_to_dict(plan)})
        executor = PlanExecutor(stack.runner, planner)
        try:
            async for event in executor.execute_streamed(
                stack.agent, plan, session_id=self._active_session
            ):
                frame = serialize_event(event)
                if frame is not None:
                    await self._emit(frame)
        except RunPaused as exc:
            await self._handle_pause(exc.state)
        except asyncio.CancelledError:
            await self._emit({"type": "run_cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("plan execution failed")
            await self._emit(
                {
                    "type": "run_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            if asyncio.current_task() is self._run_task:
                self._run_task = None

    async def _resume_checkpoint_coro(self, checkpoint_id: str) -> None:
        state = await self._store.sessions.load_checkpoint(checkpoint_id)
        if state is None:
            await self._emit(
                {
                    "type": "run_error",
                    "message": f"no such checkpoint: {checkpoint_id}",
                    "error_type": "checkpoint_not_found",
                }
            )
            return
        self._last_checkpoint_id = checkpoint_id
        await self._resume_task_coro(state)

    async def _resume_task_coro(self, state: RunState) -> None:
        stack = self.stack
        if state.session_id is not None and state.session_id != self._active_session:
            self._active_session = state.session_id
            await self._emit({"type": "session_switched", "session_id": state.session_id})
        await self._emit({"type": "resumed", "checkpoint_id": self._last_checkpoint_id})
        try:
            async for event in stack.runner.resume_streamed(
                stack.agent, state, session_id=state.session_id or self._active_session
            ):
                frame = serialize_event(event)
                if frame is not None:
                    await self._emit(frame)
        except RunPaused as exc:
            await self._handle_pause(exc.state)
        except asyncio.CancelledError:
            await self._emit({"type": "run_cancelled"})
            raise
        except MaxTurnsExceeded:
            await self._emit(
                {"type": "run_error", "message": "max turns exceeded", "error_type": "max_turns"}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("resume failed")
            await self._emit(
                {
                    "type": "run_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            if asyncio.current_task() is self._run_task:
                self._run_task = None

    async def _handle_pause(self, state: RunState) -> None:
        checkpoint_id = await self._store.sessions.save_checkpoint(state)
        self._last_state = state
        self._last_checkpoint_id = checkpoint_id
        await self._emit(
            {
                "type": "paused",
                "checkpoint_id": checkpoint_id,
                "turns": state.turns,
                "session_id": state.session_id,
            }
        )


def build_runtime(
    settings: Settings,
    store: Store,
    *,
    provider: LLMProvider | None = None,
    tool_executor: ToolExecutor | None = None,
    approval_timeout: float = APPROVAL_TIMEOUT,
    active_session: str | None = None,
) -> Runtime:
    """Construct a :class:`Runtime` — the seam tests override to inject fakes."""
    return Runtime(
        settings,
        store,
        provider=provider,
        tool_executor=tool_executor,
        approval_timeout=approval_timeout,
        active_session=active_session,
    )
