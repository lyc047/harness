"""Tracer (JSONL observability) unit + runner-integration tests."""

from __future__ import annotations

import io
import json

import pytest

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.run_result import RunResult
from harness.core.runner import Runner
from harness.llm.base import LLMResponse
from harness.observability.tracing import Tracer
from harness.tools.base import tool


@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _registry():
    from harness.tools.registry import ToolRegistry

    r = ToolRegistry()
    r.register(add)
    return r


def _run_end(tracer: Tracer) -> dict | None:
    return next((e for e in tracer.events if e["type"] == "run_end"), None)


async def test_tracer_records_callbacks_in_memory() -> None:
    tracer = Tracer()  # no stream -> events accumulate in memory
    hooks = tracer.make_hooks()
    agent = Agent(name="probe", instructions="sys", tools=_registry(), model="m")

    await hooks.on_run_start(agent)
    await hooks.on_turn_start(0, agent)
    call = ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 2}')
    await hooks.on_tool_call(call, agent)
    from harness.tools.base import ToolResult

    await hooks.on_tool_result(call, ToolResult.ok("3"), agent)
    await hooks.on_final(RunResult(final_output="3", messages=[], turns=2, session_id="s1"))

    types = [e["type"] for e in tracer.events]
    assert types == ["run_start", "turn_start", "tool_call", "tool_result", "run_end"]

    start = tracer.events[0]
    assert start["agent"] == "probe"
    assert start["model"] == "m"
    assert "add" in start["tools"]

    end = _run_end(tracer)
    assert end is not None
    assert end["turns"] == 2
    assert end["session_id"] == "s1"
    assert end["final"] == "3"
    # Every event carries a timestamp.
    assert all("ts" in e for e in tracer.events)


async def test_tracer_stream_writes_jsonl() -> None:
    buf = io.StringIO()
    tracer = Tracer(buf)
    hooks = tracer.make_hooks()
    agent = Agent(name="a", instructions="i", tools=_registry(), model="m")
    await hooks.on_run_start(agent)

    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "run_start"
    assert entry["agent"] == "a"
    assert entry["ts"]
    tracer.close()


async def test_tracer_trims_long_content() -> None:
    tracer = Tracer(max_content_chars=20)
    hooks = tracer.make_hooks()
    long_text = "x" * 1000
    await hooks.on_text(long_text)

    entry = tracer.events[0]
    assert entry["type"] == "text"
    assert entry["text"] is not None
    assert len(entry["text"]) <= 20 + 40  # trimmed prefix + ellipsis suffix
    assert "... (1000 chars)" in entry["text"]


async def test_tracer_run_end_records_error_flag_on_tool_result() -> None:
    from harness.tools.base import ToolResult

    tracer = Tracer()
    hooks = tracer.make_hooks()
    agent = Agent(name="a", instructions="i", tools=_registry(), model="m")
    call = ToolCall(id="c1", name="add", arguments='{"a": 1}')
    await hooks.on_tool_result(call, ToolResult.error("boom"), agent)

    entry = tracer.events[0]
    assert entry["type"] == "tool_result"
    assert entry["is_error"] is True
    assert entry["name"] == "add"
    assert entry["content"] == "boom"


async def test_tracer_attached_to_runner_loop(make_provider) -> None:
    provider = make_provider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 2}')]),
            LLMResponse(final_text="3"),
        ]
    )
    tracer = Tracer()
    runner = Runner(provider, hooks=tracer.make_hooks())
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)

    result = await runner.run(agent, "1+2?")
    assert result.final_output == "3"

    types = [e["type"] for e in tracer.events]
    assert "run_start" in types
    assert "turn_start" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "run_end" in types
    assert _run_end(tracer)["turns"] == 2


@pytest.mark.asyncio
async def test_tracer_handles_agent_none_for_tool_hooks() -> None:
    tracer = Tracer()
    hooks = tracer.make_hooks()
    call = ToolCall(id="c1", name="bash", arguments="{}")
    await hooks.on_tool_call(call, None)
    assert tracer.events[0]["name"] == "bash"
