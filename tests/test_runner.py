"""Runner turn-loop behaviour with a scripted fake provider."""

import pytest

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.run_result import MaxTurnsExceeded, RunResult
from harness.core.runner import RunDone, Runner, ToolResultEvent
from harness.llm.base import LLMResponse
from harness.tools.base import ToolResult, tool


@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _registry():
    from harness.tools.registry import ToolRegistry

    r = ToolRegistry()
    r.register(add)
    return r


@pytest.mark.asyncio
async def test_run_with_tool_roundtrip(make_provider):
    provider = make_provider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 2}')]),
            LLMResponse(final_text="3"),
        ]
    )
    runner = Runner(provider)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    result = await runner.run(agent, "what is 1+2?")

    assert isinstance(result, RunResult)
    assert result.final_output == "3"
    assert result.turns == 2
    # The tool call + result are part of the message history.
    roles = [m.role for m in result.messages]
    assert "tool" in roles
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "3"


@pytest.mark.asyncio
async def test_stream_yields_tool_events(make_provider):
    provider = make_provider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments='{"a": 2, "b": 3}')]),
            LLMResponse(final_text="5"),
        ]
    )
    runner = Runner(provider)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)

    saw_tool_event = False
    async for event in runner.run_streamed(agent, "1+2?"):
        if isinstance(event, ToolResultEvent):
            saw_tool_event = True
            assert event.result.content == "5"
        if isinstance(event, RunDone):
            assert event.result.final_output == "5"
    assert saw_tool_event


@pytest.mark.asyncio
async def test_max_turns_exceeded(make_provider):
    # Model keeps calling tools forever -> loop must stop.
    forever = [
        LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="add", arguments='{"a": 1, "b": 1}')])
        for i in range(100)
    ]
    provider = make_provider(script=forever)
    runner = Runner(provider)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=3)
    with pytest.raises(MaxTurnsExceeded):
        await runner.run(agent, "go")


@pytest.mark.asyncio
async def test_unknown_tool_is_an_error_result(make_provider):
    provider = make_provider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="does_not_exist", arguments="{}")]),
            LLMResponse(final_text="done"),
        ]
    )
    runner = Runner(provider)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    result = await runner.run(agent, "hi")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "unknown tool" in tool_msg.content


@pytest.mark.asyncio
async def test_session_persistence(tmp_path, make_provider):
    from harness.memory.session import SessionStore

    store = SessionStore(str(tmp_path / "test.db"))
    await store.initialize()
    session = await store.create_session()

    provider = make_provider(script=[LLMResponse(final_text="hello back")])
    runner = Runner(provider, session_store=store)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    await runner.run(agent, "hello", session_id=session.id)

    loaded = await store.load_messages(session.id)
    contents = [m.content for m in loaded]
    assert "hello" in contents
    assert "hello back" in contents
    # System prompt leads the persisted history.
    assert loaded[0].role == "system"
    await store.close()


@pytest.mark.asyncio
async def test_resume_adds_system_only_once(tmp_path, make_provider):
    from harness.memory.session import SessionStore

    store = SessionStore(str(tmp_path / "resume.db"))
    await store.initialize()
    session = await store.create_session()

    provider = make_provider(
        script=[LLMResponse(final_text="first"), LLMResponse(final_text="second")]
    )
    runner = Runner(provider, session_store=store)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)

    await runner.run(agent, "q1", session_id=session.id)
    await runner.run(agent, "q2", session_id=session.id)

    loaded = await store.load_messages(session.id)
    assert [m.role for m in loaded].count("system") == 1
    assert [m.content for m in loaded].count("q1") == 1
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_tool_calls_preserve_order(make_provider):
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 1}'),
                ToolCall(id="c2", name="add", arguments='{"a": 2, "b": 2}'),
                ToolCall(id="c3", name="add", arguments='{"a": 3, "b": 3}'),
            ]
        ),
        LLMResponse(final_text="done"),
    ]
    runner = Runner(make_provider(script=script))
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    results: list[ToolResult] = []
    async for event in runner.run_streamed(agent, "sum them", concurrent=True):
        if isinstance(event, ToolResultEvent):
            results.append(event.result)
    assert [r.content for r in results] == ["2", "4", "6"]


@pytest.mark.asyncio
async def test_concurrent_one_failure_keeps_others(make_provider):
    from harness.core.runner import default_executor

    async def flaky(agent, tool_call):
        if tool_call.id == "c2":
            raise RuntimeError("boom")
        return await default_executor(agent, tool_call)

    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 1}'),
                ToolCall(id="c2", name="add", arguments='{"a": 9, "b": 9}'),
                ToolCall(id="c3", name="add", arguments='{"a": 3, "b": 3}'),
            ]
        ),
        LLMResponse(final_text="done"),
    ]
    runner = Runner(make_provider(script=script), tool_executor=flaky)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    results: list[ToolResult] = []
    async for event in runner.run_streamed(agent, "sum them", concurrent=True):
        if isinstance(event, ToolResultEvent):
            results.append(event.result)
    assert [r.content for r in results] == ["2", "RuntimeError: boom", "6"]
    assert [r.is_error for r in results] == [False, True, False]
