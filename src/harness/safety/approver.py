"""Approval flow that wraps tool execution (human-in-the-loop).

An :class:`ApprovalExecutor` sits in front of a real tool executor and consults
a :class:`Permissions` policy. ALLOW decisions pass straight through; DENY and
no-approver cases fail closed and return an error result to the model so it can
adapt; ASK decisions prompt a human through an injected async callable.

The prompt callback returns one of::

    "y"         allow once
    "n"         deny — the call is blocked and reported to the model
    "a"         allow for the rest of this session
    "p"         allow once and pause after this turn (via ``on_pause``)
    "e:<json>"  allow with edited arguments (the tool is invoked with them)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.observability.logging import get_logger
from harness.safety.permissions import Permission, Permissions
from harness.tools.base import ToolResult

logger = get_logger("safety")

ApprovalPrompt = Callable[[ToolCall], Awaitable[str]]


class ApprovalExecutor:
    """Wrap a :class:`ToolExecutor` with permission checks and approval."""

    def __init__(
        self,
        inner: ToolExecutor,
        permissions: Permissions,
        *,
        prompt: ApprovalPrompt | None = None,
        on_pause: Callable[[], None] | None = None,
    ) -> None:
        self._inner = inner
        self._permissions = permissions
        self._prompt = prompt
        self._on_pause = on_pause
        self._session_allowed: set[str] = set()

    async def __call__(self, agent: Agent, tool_call: ToolCall) -> ToolResult:
        decision = self._permissions.decide(tool_call)
        if decision is Permission.ALLOW:
            return await self._inner(agent, tool_call)
        if decision is Permission.DENY:
            return ToolResult.error(
                f"blocked by permission policy (tool {tool_call.name!r} is denied)"
            )

        # ASK
        if tool_call.name in self._session_allowed:
            return await self._inner(agent, tool_call)
        if self._prompt is None:
            logger.warning("approval required for %r but no approver attached", tool_call.name)
            return ToolResult.error(
                f"approval required for {tool_call.name!r} but no approver is "
                "available; call blocked (fail-closed)"
            )

        choice = (await self._prompt(tool_call)).strip()
        if choice == "a":
            self._session_allowed.add(tool_call.name)
            return await self._inner(agent, tool_call)
        if choice == "y":
            return await self._inner(agent, tool_call)
        if choice == "p":
            if self._on_pause is not None:
                self._on_pause()
            return await self._inner(agent, tool_call)
        if choice.startswith("e:"):
            edited = choice[2:].strip()
            try:
                json.loads(edited)
            except json.JSONDecodeError:
                return ToolResult.error("edited arguments are not valid JSON; call blocked")
            return await self._inner(agent, replace(tool_call, arguments=edited))
        # "n" or anything unrecognised -> fail closed
        logger.info("tool %r denied by approver", tool_call.name)
        return ToolResult.error("blocked by user (approval denied)")
