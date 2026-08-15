"""OffloadExecutor: write oversized tool results to disk and hand the model a
small reference (path + preview) instead of the full output.

Not a :class:`ToolExecutor` — ``session_id`` is a per-run argument, so the
Runner binds it per run (see ``Runner._run_streamed``). ``process`` returns
the (possibly replaced) :class:`ToolResult`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from harness.context.store import ContextStore, estimate_tokens
from harness.core.messages import ToolCall
from harness.tools.base import ToolResult

PREVIEW_LINES = 10
PREVIEW_CHARS = 2_000


def _preview(content: str) -> str:
    head = "\n".join(content.splitlines()[:PREVIEW_LINES])
    if len(head) > PREVIEW_CHARS:
        head = head[:PREVIEW_CHARS] + "…"
    return head


class OffloadExecutor:
    """Post-processes a tool result, offloading oversized output to disk."""

    def __init__(
        self,
        store: ContextStore,
        *,
        threshold: int = 20_000,
        token_estimator: Callable[[str], int] = estimate_tokens,
    ) -> None:
        self._store = store
        self._threshold = threshold
        self._token_estimator = token_estimator

    async def process(
        self, session_id: str | None, tool_call: ToolCall, result: ToolResult
    ) -> ToolResult:
        if session_id is None:
            return result
        if result.is_error:
            return result
        if self._token_estimator(result.content) <= self._threshold:
            return result
        n = self._token_estimator(result.content)
        path = self._store.offload(session_id, tool_call.id, result.content)
        rel = self._store.relpath(path)
        content = f"[offloaded to {rel} — ~{n} tokens]\n{_preview(result.content)}"
        return replace(result, content=content, metadata={**result.metadata, "offloaded": rel})
