"""SnapshotExecutor: records pre-write file state for rollback support."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.core.messages import ToolCall
from harness.core.snapshot import SnapshotExecutor
from harness.memory.session import SessionStore
from harness.tools.base import ToolResult


@pytest.fixture
async def store(tmp_path):
    s = SessionStore(str(tmp_path / "snapshots.db"))
    await s.initialize()
    yield s
    await s.close()


def _write(path: str, content: str) -> None:
    """Sync file write (ASYNC240-safe for the test coroutines)."""
    Path(path).write_text(content, encoding="utf-8")


async def _inner(agent, tool_call):  # noqa: ARG001
    """Real write_file behavior: write content to disk."""
    args = tool_call.arguments_dict
    _write(str(args["path"]), str(args.get("content", "")))
    return ToolResult.ok("wrote")


def _write_call(path: str, content: str, tool_id: str = "tc1") -> ToolCall:
    return ToolCall(
        id=tool_id,
        name="write_file",
        arguments=json.dumps({"path": path, "content": content}),
    )


@pytest.mark.asyncio
async def test_snapshot_overwrite_records_old_content(store, tmp_path):
    target = tmp_path / "a.txt"
    _write(str(target), "v1")
    ex = SnapshotExecutor(_inner, store)
    result = await ex(None, _write_call(str(target), "v2"))  # type: ignore[arg-type]
    assert result.is_error is False
    snap = await store.load_file_snapshot("tc1")
    assert snap == {"path": str(target), "content": "v1", "existed": True}
    # the actual write went through
    assert target.read_text(encoding="utf-8") == "v2"


@pytest.mark.asyncio
async def test_snapshot_new_file_records_not_existed(store, tmp_path):
    target = tmp_path / "new.txt"
    ex = SnapshotExecutor(_inner, store)
    await ex(None, _write_call(str(target), "hello"))  # type: ignore[arg-type]
    snap = await store.load_file_snapshot("tc1")
    assert snap == {"path": str(target), "content": None, "existed": False}


@pytest.mark.asyncio
async def test_snapshot_skipped_on_write_error(store, tmp_path):
    async def failing_inner(agent, tool_call):  # noqa: ARG001
        return ToolResult.error("disk full")

    target = tmp_path / "a.txt"
    ex = SnapshotExecutor(failing_inner, store)
    result = await ex(None, _write_call(str(target), "x"))  # type: ignore[arg-type]
    assert result.is_error is True
    assert await store.load_file_snapshot("tc1") is None


@pytest.mark.asyncio
async def test_non_write_tool_passes_through(store):
    seen: list[str] = []

    async def inner(agent, tool_call):  # noqa: ARG001
        seen.append(tool_call.name)
        return ToolResult.ok("ran")

    tc = ToolCall(id="t2", name="bash", arguments='{"command": "ls"}')
    ex = SnapshotExecutor(inner, store)
    result = await ex(None, tc)  # type: ignore[arg-type]
    assert result.content == "ran"
    assert seen == ["bash"]
    assert await store.load_file_snapshot("t2") is None
