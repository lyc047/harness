"""Sandbox abstraction.

A :class:`SandboxProvider` runs shell commands in an isolated environment.
The local implementation is the development default (no isolation); remote
providers (SSH) run on a rented server so local filesystem side effects are
avoided. ``SandboxedExecutor`` routes the agent's ``bash`` tool calls through
the configured provider, keeping the model-visible tool interface unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.tools.base import ToolResult


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of running a command inside a sandbox."""

    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False

    def to_text(self, *, command: str | None = None) -> str:
        """Render the result the way the builtin bash tool formats its output."""
        parts: list[str] = []
        if command:
            parts.append(f"$ {command}")
        if self.timed_out:
            parts.append("result: timed out")
        else:
            parts.append(f"exit code: {self.exit_code}")
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        return "\n".join(parts)


class SandboxProvider(Protocol):
    """Runs a single shell command in an isolated environment."""

    async def run_command(
        self, command: str, *, timeout: float | None = None
    ) -> SandboxResult: ...

    async def check_available(self) -> bool: ...


class SandboxedExecutor:
    """Wrap a :class:`ToolExecutor`, routing ``bash`` calls to a sandbox.

    Other tools pass through to the wrapped executor unchanged.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        sandbox: SandboxProvider,
        *,
        tool_name: str = "bash",
        default_timeout: float = 60.0,
    ) -> None:
        self._inner = inner
        self._sandbox = sandbox
        self._tool_name = tool_name
        self._default_timeout = default_timeout

    @property
    def sandbox(self) -> SandboxProvider:
        return self._sandbox

    async def __call__(self, agent: Agent, tool_call: ToolCall) -> ToolResult:
        if tool_call.name != self._tool_name:
            return await self._inner(agent, tool_call)

        args = tool_call.arguments_dict
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.error("empty command")
        try:
            timeout = float(args.get("timeout", self._default_timeout))
        except (TypeError, ValueError):
            timeout = self._default_timeout

        result = await self._sandbox.run_command(command, timeout=timeout)
        return ToolResult(
            content=result.to_text(command=command),
            is_error=result.exit_code != 0 or result.timed_out,
        )
