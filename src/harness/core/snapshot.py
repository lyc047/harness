"""Pre-write file snapshots so the workspace can be rolled back.

Every successful ``write_file`` records the target's pre-write state (old
content, or "did not exist"), keyed by ``tool_call_id``. A rollback to an
earlier conversation point restores all snapshots whose writes happened after
that point — undoing the code changes the agent made. ``bash`` mutations are
intentionally not tracked (a raw command string gives no reliable per-file
signal).
"""

from __future__ import annotations

from pathlib import Path

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.memory.session import SessionStore
from harness.tools.base import ToolResult


def _read_old_state(path: str) -> tuple[bool, str | None]:
    """Sync helper (kept out of the async executor for ASYNC240). Returns
    ``(existed, old_content)``; content is None when unreadable."""
    p = Path(path)
    if not p.exists():
        return False, None
    try:
        return True, p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True, None


class SnapshotExecutor:
    """Record a pre-write snapshot for every successful ``write_file``.

    Sits at the innermost position of the executor chain — after approval,
    before the actual write — so it snapshots exactly the writes that happen.
    Other tools pass through unchanged.
    """

    def __init__(self, inner: ToolExecutor, session_store: SessionStore) -> None:
        self._inner = inner
        self._store = session_store

    async def __call__(self, agent: Agent, tool_call: ToolCall) -> ToolResult:
        if tool_call.name != "write_file":
            return await self._inner(agent, tool_call)
        path = str(tool_call.arguments_dict.get("path", ""))
        if not path:
            return await self._inner(agent, tool_call)
        existed, old_content = _read_old_state(path)
        result = await self._inner(agent, tool_call)
        if not result.is_error:
            await self._store.save_file_snapshot(
                tool_call.id, path, old_content, existed
            )
        return result
