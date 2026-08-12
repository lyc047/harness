"""Unit tests for FileLockExecutor (per-path mutual exclusion)."""

from __future__ import annotations

import asyncio

import pytest

from harness.core.agent import Agent
from harness.core.locking import FileLockExecutor
from harness.core.messages import ToolCall
from harness.tools.base import ToolResult


def _agent() -> Agent:
    return Agent(name="a", instructions="i", model="m")


@pytest.mark.asyncio
async def test_same_path_writes_serialize() -> None:
    in_flight = 0
    max_in_flight = 0

    async def inner(agent, tool_call):  # noqa: ARG001
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ToolResult.ok("done")

    ex = FileLockExecutor(inner)
    calls = [
        ToolCall(id="a", name="write_file", arguments='{"path": "x.txt", "content": "1"}'),
        ToolCall(id="b", name="write_file", arguments='{"path": "x.txt", "content": "2"}'),
    ]
    await asyncio.gather(*(ex(_agent(), tc) for tc in calls))
    assert max_in_flight == 1  # never two writers inside at once


@pytest.mark.asyncio
async def test_different_paths_run_in_parallel() -> None:
    in_flight = 0
    max_in_flight = 0

    async def inner(agent, tool_call):  # noqa: ARG001
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ToolResult.ok("done")

    ex = FileLockExecutor(inner)
    calls = [
        ToolCall(id="a", name="write_file", arguments='{"path": "x.txt", "content": "1"}'),
        ToolCall(id="b", name="write_file", arguments='{"path": "y.txt", "content": "2"}'),
    ]
    await asyncio.gather(*(ex(_agent(), tc) for tc in calls))
    assert max_in_flight == 2  # different paths do not block each other


@pytest.mark.asyncio
async def test_read_waits_for_write_on_same_path() -> None:
    events: list[str] = []

    async def inner(agent, tool_call):  # noqa: ARG001
        if tool_call.name == "write_file":
            events.append("write-start")
            await asyncio.sleep(0.01)
            events.append("write-end")
        else:
            events.append("read")
        return ToolResult.ok("ok")

    ex = FileLockExecutor(inner)
    calls = [
        ToolCall(id="w", name="write_file", arguments='{"path": "f", "content": "x"}'),
        ToolCall(id="r", name="read_file", arguments='{"path": "f"}'),
    ]
    await asyncio.gather(*(ex(_agent(), tc) for tc in calls))
    # the read must be fully outside the write's critical section
    assert events.index("read") > events.index("write-end")


@pytest.mark.asyncio
async def test_non_file_tools_pass_through() -> None:
    seen: list[str] = []

    async def inner(agent, tool_call):  # noqa: ARG001
        seen.append(tool_call.id)
        return ToolResult.ok("ok")

    ex = FileLockExecutor(inner)
    await ex(_agent(), ToolCall(id="b", name="bash", arguments='{"command": "ls"}'))
    assert seen == ["b"]
