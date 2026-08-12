"""Per-path file locking so concurrent file access serializes per path.

A process-global registry of ``asyncio.Lock`` keyed by the resolved absolute
path. ``FileLockExecutor`` sits between the sandbox and the snapshot executor:
for ``write_file``/``read_file`` it acquires the path's lock, making
snapshot+write atomic and giving every path a single writer (readers wait).
``bash`` is intentionally not covered — the sandbox handles it before reaching
this layer, and a raw command string gives no reliable per-file signal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.tools.base import ToolResult

_LOCKS: dict[str, asyncio.Lock] = {}


def _path_lock(path: str) -> asyncio.Lock:
    key = str(Path(path).resolve())
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


class FileLockExecutor:
    """Serialize per-path file access under the process-global lock registry."""

    def __init__(self, inner: ToolExecutor) -> None:
        self._inner = inner

    async def __call__(self, agent: Agent, tool_call: ToolCall) -> ToolResult:
        if tool_call.name not in ("read_file", "write_file"):
            return await self._inner(agent, tool_call)
        path = str(tool_call.arguments_dict.get("path", ""))
        if not path:
            return await self._inner(agent, tool_call)
        async with _path_lock(path):
            return await self._inner(agent, tool_call)
