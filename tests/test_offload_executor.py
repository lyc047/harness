"""OffloadExecutor: oversized tool results go to disk, context gets a reference."""

from __future__ import annotations

import pytest

from harness.context.offload import OffloadExecutor
from harness.context.store import ContextStore
from harness.core.messages import ToolCall
from harness.tools.base import ToolResult


def _call(session_id, result, store, *, threshold=20_000):
    offload = OffloadExecutor(store, threshold=threshold)
    return result


@pytest.mark.asyncio
async def test_offloads_oversized_result(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    big = "line one\n" + "x" * 4000  # 单行超预览窗口（2K）：引用必须截断全文
    result = await offload.process(
        "sess1", ToolCall(id="c1", name="bash", arguments="{}"), ToolResult.ok(big)
    )
    # 上下文里是引用而非全文
    assert "x" * 4000 not in result.content
    assert result.content.count("x") < 4000
    assert "[offloaded to" in result.content
    assert "offload_c1.txt" in result.content
    # 全文落盘
    assert (tmp_path / "sess1" / "offload_c1.txt").read_text(encoding="utf-8") == big
    assert result.metadata["offloaded"] == "sess1/offload_c1.txt"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_small_result_passthrough(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    result = await offload.process(
        "sess1", ToolCall(id="c2", name="bash", arguments="{}"), ToolResult.ok("tiny")
    )
    assert result.content == "tiny"
    assert "offloaded" not in result.metadata
    assert not (tmp_path / "sess1").exists()


@pytest.mark.asyncio
async def test_error_result_not_offloaded(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    result = await offload.process(
        "sess1", ToolCall(id="c3", name="bash", arguments="{}"),
        ToolResult.error("e" * 500),
    )
    assert result.content == "e" * 500
    assert result.is_error is True
    assert not (tmp_path / "sess1").exists()


@pytest.mark.asyncio
async def test_none_session_skips(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    result = await offload.process(
        None, ToolCall(id="c4", name="bash", arguments="{}"), ToolResult.ok("x" * 500)
    )
    assert result.content == "x" * 500
