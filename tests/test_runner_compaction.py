"""Runner integration: turn-boundary compaction + per-run offload binding."""

from __future__ import annotations

import json

import pytest

from harness.context.compactor import ContextCompactor
from harness.context.offload import OffloadExecutor
from harness.context.store import ContextStore
from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import CompactionEvent, Runner
from harness.llm.base import LLMResponse
from harness.tools.base import tool
from harness.tools.registry import ToolRegistry

from conftest import FakeProvider


class _CapturingProvider(FakeProvider):
    """FakeProvider that also records the message list of every stream() call."""

    def __init__(self, script=None):
        super().__init__(script)
        self.seen: list[list] = []

    async def stream(self, messages, *, tools=None, model=None):
        self.seen.append(list(messages))
        async for e in super().stream(messages, tools=tools, model=model):
            yield e


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


def _registry():
    r = ToolRegistry()
    r.register(echo)
    return r


@pytest.mark.asyncio
async def test_compaction_at_turn_boundary(tmp_path):
    ctx = ContextStore(tmp_path / "ctx")
    summary_provider = FakeProvider(script=[LLMResponse(final_text="summarized.")])
    compactor = ContextCompactor(ctx, summary_provider, window=50, trigger=1.0, keep=2)

    main_provider = _CapturingProvider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "x" * 200}))]),
            LLMResponse(final_text="done"),
        ]
    )
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    runner = Runner(main_provider, compactor=compactor)

    events = [e async for e in runner.run_streamed(agent, "hello", session_id="s1")]
    compacted = [e for e in events if isinstance(e, CompactionEvent)]
    assert len(compacted) == 1
    assert compacted[0].transcript_path.startswith("s1/transcript_")
    # turn 1 的 model call 看到压缩后历史（system + 摘要 + ≤keep 条）
    assert len(main_provider.seen) == 2
    assert len(main_provider.seen[1]) <= 4
    assert (tmp_path / "ctx" / "s1").exists()


@pytest.mark.asyncio
async def test_offload_binding_reduces_context(tmp_path):
    ctx = ContextStore(tmp_path / "ctx")
    offload = OffloadExecutor(ctx, threshold=10)
    provider = _CapturingProvider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "x" * 4000}))]),
            LLMResponse(final_text="done"),
        ]
    )
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    runner = Runner(provider, offload_processor=offload)

    events = [e async for e in runner.run_streamed(agent, "hello", session_id="s1")]
    # 第二次 model call 里工具消息是引用而非全文
    assert len(provider.seen) == 2
    tool_msg = next(m for m in provider.seen[1] if m.role == "tool")
    assert "x" * 4000 not in tool_msg.content
    assert "[offloaded to" in tool_msg.content
    assert list((tmp_path / "ctx" / "s1").glob("offload_*.txt"))


@pytest.mark.asyncio
async def test_no_context_no_change(tmp_path):
    provider = _CapturingProvider(script=[LLMResponse(final_text="plain")])
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    runner = Runner(provider)  # 不注入 compactor / offload
    events = [e async for e in runner.run_streamed(agent, "hi", session_id="s1")]
    assert not [e for e in events if isinstance(e, CompactionEvent)]
    assert len(provider.seen) == 1
