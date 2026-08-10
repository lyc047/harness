"""Tests for sandbox providers and the bash-routing executor."""

from __future__ import annotations

import pytest

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.sandbox import (
    LocalSandbox,
    SandboxedExecutor,
    SandboxResult,
    SSHSandbox,
    build_sandbox,
)
from harness.tools.base import ToolResult


class FakeSandbox:
    def __init__(self, result: SandboxResult | None = None) -> None:
        self.result = result or SandboxResult(exit_code=0, stdout="ok")
        self.commands: list[str] = []

    async def run_command(self, command: str, *, timeout: float | None = None) -> SandboxResult:
        self.commands.append(command)
        return self.result

    async def check_available(self) -> bool:
        return True


class FakeSSHTransport:
    def __init__(self, result: SandboxResult | None = None) -> None:
        self.result = result or SandboxResult(exit_code=0, stdout="remote-out")
        self.commands: list[str] = []

    def exec(self, command: str, timeout: float | None = None) -> SandboxResult:
        self.commands.append(command)
        return self.result


def _agent() -> Agent:
    return Agent(name="a", instructions="i", model="m")


# -- local sandbox --

async def test_local_sandbox_run() -> None:
    sandbox = LocalSandbox()
    result = await sandbox.run_command("echo hi")
    assert result.exit_code == 0
    assert "hi" in result.stdout


async def test_local_sandbox_error_exit_code() -> None:
    sandbox = LocalSandbox()
    result = await sandbox.run_command("exit 3")
    assert result.exit_code == 3
    assert not result.timed_out


async def test_local_sandbox_timeout() -> None:
    sandbox = LocalSandbox()
    # "sleep 30" via python so the command blocks regardless of shell (cmd vs sh).
    result = await sandbox.run_command(
        'python -c "import time; time.sleep(30)"', timeout=0.5
    )
    assert result.timed_out
    assert result.exit_code != 0


# -- SandboxedExecutor --

async def test_sandboxed_executor_routes_bash() -> None:
    sandbox = FakeSandbox()
    executor = SandboxedExecutor(_noop_inner(), sandbox)  # type: ignore[arg-type]
    result = await executor(
        _agent(), ToolCall(id="c1", name="bash", arguments='{"command": "echo hi"}')
    )
    assert sandbox.commands == ["echo hi"]
    assert not result.is_error
    assert "$ echo hi" in result.content
    assert "ok" in result.content


async def test_sandboxed_executor_passthrough_other_tools() -> None:
    sandbox = FakeSandbox()
    inner_calls: list[ToolCall] = []

    async def inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        inner_calls.append(tool_call)
        return ToolResult.ok("inner")

    executor = SandboxedExecutor(inner, sandbox)  # type: ignore[arg-type]
    result = await executor(
        _agent(), ToolCall(id="c1", name="read_file", arguments='{"path": "a.txt"}')
    )
    assert result.content == "inner"
    assert len(inner_calls) == 1
    assert sandbox.commands == []


async def test_sandboxed_executor_error_flag_on_nonzero() -> None:
    sandbox = FakeSandbox(SandboxResult(exit_code=1, stdout="", stderr="boom"))
    executor = SandboxedExecutor(_noop_inner(), sandbox)  # type: ignore[arg-type]
    call = ToolCall(id="c1", name="bash", arguments='{"command": "false"}')
    result = await executor(_agent(), call)
    assert result.is_error
    assert "exit code: 1" in result.content


async def test_sandboxed_executor_empty_command() -> None:
    sandbox = FakeSandbox()
    executor = SandboxedExecutor(_noop_inner(), sandbox)  # type: ignore[arg-type]
    result = await executor(_agent(), ToolCall(id="c1", name="bash", arguments="{}"))
    assert result.is_error
    assert "empty command" in result.content
    assert sandbox.commands == []


def _noop_inner():
    async def inner(agent: Agent, tool_call: ToolCall) -> ToolResult:
        return ToolResult.ok("unused")

    return inner


# -- SSH sandbox (fake transport) --

def test_ssh_remote_command_build() -> None:
    sandbox = SSHSandbox(host="h", workdir="~/workspace")
    cmd = sandbox._build_remote_command("ls -la")
    assert cmd == "mkdir -p ~/workspace && cd ~/workspace && ls -la"


async def test_ssh_sandbox_with_fake_transport() -> None:
    transport = FakeSSHTransport()
    sandbox = SSHSandbox(  # type: ignore[arg-type]
        host="h", user="u", workdir="~/ws", transport=transport
    )
    result = await sandbox.run_command("echo hi")
    assert result.stdout == "remote-out"
    assert transport.commands == ["echo hi"]


async def test_ssh_check_available_fake() -> None:
    sandbox = SSHSandbox(host="h", transport=FakeSSHTransport())  # type: ignore[arg-type]
    assert await sandbox.check_available() is True


async def test_ssh_timeout_via_transport() -> None:
    class TimeoutTransport:
        def exec(self, command: str, timeout: float | None = None) -> SandboxResult:
            raise TimeoutError("remote timeout")

    sandbox = SSHSandbox(host="h", transport=TimeoutTransport())  # type: ignore[arg-type]
    result = await sandbox.run_command("sleep 5", timeout=0.1)
    assert result.timed_out
    assert "timed out" in result.stderr


async def test_ssh_exception_fails_gracefully() -> None:
    class BrokenTransport:
        def exec(self, command: str, timeout: float | None = None) -> SandboxResult:
            raise ConnectionError("down")

    sandbox = SSHSandbox(host="h", transport=BrokenTransport())  # type: ignore[arg-type]
    result = await sandbox.run_command("ls")
    assert result.exit_code != 0
    assert "ssh error" in result.stderr


# -- factory --

def test_build_sandbox_local() -> None:
    sandbox = build_sandbox(Settings(sandbox_mode="local"))
    assert isinstance(sandbox, LocalSandbox)


def test_build_sandbox_ssh_missing_host() -> None:
    with pytest.raises(ValueError):
        build_sandbox(Settings(sandbox_mode="ssh"))


def test_build_sandbox_ssh() -> None:
    sandbox = build_sandbox(
        Settings(sandbox_mode="ssh", sandbox_host="example.com", sandbox_user="u")
    )
    assert isinstance(sandbox, SSHSandbox)


def test_sandbox_result_to_text() -> None:
    result = SandboxResult(exit_code=0, stdout="line1\nline2")
    text = result.to_text(command="ls")
    assert "$ ls" in text
    assert "exit code: 0" in text
    assert "line1" in text
    assert "line2" in text
