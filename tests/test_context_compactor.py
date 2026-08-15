"""ContextCompactor: auto-summarize at window trigger, on-demand via request."""

from __future__ import annotations

import pytest

from harness.context.compactor import (
    CompactRequest,
    CompactionResult,
    ContextCompactor,
    make_compact_conversation_tool,
)
from harness.context.store import ContextStore
from harness.core.messages import Message
from harness.llm.base import LLMResponse
from harness.tools.base import Tool


class _FakeComplete:
    """Minimal provider: only ``complete`` (compactor needs nothing else)."""

    def __init__(self, script=None, *, raise_on_complete=False):
        self.script = list(script or [])
        self.raise_on_complete = raise_on_complete

    async def complete(self, messages, *, tools=None, model=None):
        if self.raise_on_complete:
            raise RuntimeError("boom")
        resp = self.script.pop(0) if self.script else LLMResponse(final_text="(no script)")
        return resp


def _big_history(n=4, size=500):
    return [Message.system("sys")] + [Message.user("a" * size) for _ in range(n)]


@pytest.mark.asyncio
async def test_trigger_by_size(tmp_path):
    store = ContextStore(tmp_path)
    provider = _FakeComplete(script=[LLMResponse(final_text="summarized.")])
    comp = ContextCompactor(store, provider, window=100, trigger=1.0, keep=2)
    result = await comp.maybe_compact(_big_history(), session_id="s1", turn=0)
    assert result.changed is True
    msgs = result.messages
    assert msgs[0].content == "sys"                      # system 指令保留
    assert "summarized." in msgs[1].content              # 摘要消息
    assert "compacted transcript: s1/transcript_0.jsonl" in msgs[1].content
    assert len(msgs) <= 4                                # system + 摘要 + 保留 ≤ 2
    assert (tmp_path / "s1" / "transcript_0.jsonl").exists()
    assert result.freed_tokens > 0


@pytest.mark.asyncio
async def test_no_trigger_when_small(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(store, _FakeComplete(), window=1_000_000, trigger=0.85, keep=20)
    msgs = [Message.system("sys"), Message.user("hi")]
    result = await comp.maybe_compact(msgs, session_id="s1", turn=0)
    assert result.changed is False
    assert result.messages == msgs


@pytest.mark.asyncio
async def test_trigger_by_request(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(
        store, _FakeComplete(script=[LLMResponse(final_text="summarized.")]),
        window=1_000_000, trigger=0.85, keep=20,
    )
    comp.request_compaction()
    msgs = [Message.system("sys"), Message.user("hi")]
    result = await comp.maybe_compact(msgs, session_id="s1", turn=1)
    assert result.changed is True
    # 请求被消费：再次调用且无请求/小历史 → 不变
    result2 = await comp.maybe_compact(msgs, session_id="s1", turn=2)
    assert result2.changed is False


@pytest.mark.asyncio
async def test_keeps_recent_bounded_by_tokens(tmp_path):
    store = ContextStore(tmp_path)
    provider = _FakeComplete(script=[LLMResponse(final_text="summarized.")])
    comp = ContextCompactor(store, provider, window=100, trigger=1.0, keep=2)
    # 5 条大消息：token 预算 int(100*0.1)=10，任何单条 125 token 都超 → 只留最新 1 条
    msgs = [Message.system("sys")] + [Message.user("b" * 500) for _ in range(5)]
    result = await comp.maybe_compact(msgs, session_id="s1", turn=0)
    assert result.changed is True
    kept = [m for m in result.messages[2:]]
    assert len(kept) == 1


@pytest.mark.asyncio
async def test_fallback_on_provider_error(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(store, _FakeComplete(raise_on_complete=True),
                            window=100, trigger=1.0, keep=2)
    result = await comp.maybe_compact(_big_history(), session_id="s1", turn=0)
    assert result.changed is True          # 压缩仍然发生（fallback 截断）
    assert "History truncated" in result.messages[1].content


@pytest.mark.asyncio
async def test_none_session_skips(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(store, _FakeComplete(), window=100, trigger=1.0, keep=2)
    msgs = _big_history()
    result = await comp.maybe_compact(msgs, session_id=None, turn=0)
    assert result.changed is False
    assert result.messages == msgs


def test_compact_tool_sets_request():
    request = CompactRequest()
    assert request.take() is False
    tool = make_compact_conversation_tool(request)
    assert isinstance(tool, Tool)
    assert tool.name == "compact_conversation"
