"""JSONL tracing of turn/tool events for production observability.

A :class:`Tracer` records one JSON object per line (UTC timestamp + ``type``)
describing the lifecycle of a run: run start, each turn, each tool call and its
result, and the final answer. It hooks into the runner through the existing
:class:`~harness.core.hooks.Hooks` mechanism — nothing in the core loop knows
about tracing.

Usage::

    tracer = Tracer(open("harness.trace.jsonl", "w", encoding="utf-8"))
    runner = Runner(provider, hooks=tracer.make_hooks())

If no stream is given, events accumulate in ``tracer.events`` (used by tests).
Long fields are trimmed to ``max_content_chars`` so traces stay small.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TextIO

from harness.core.agent import Agent
from harness.core.hooks import Hooks
from harness.core.messages import ToolCall
from harness.core.run_result import RunResult
from harness.tools.base import ToolResult

_DEFAULT_MAX_CONTENT = 500


def _trim(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


class Tracer:
    """Records lifecycle events as JSON lines."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        max_content_chars: int = _DEFAULT_MAX_CONTENT,
    ) -> None:
        self._stream = stream
        self._max = max_content_chars
        self.events: list[dict[str, Any]] = []

    # -- event recording -- #

    def _emit(self, type_: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "type": type_,
            **fields,
        }
        if self._stream is not None:
            self._stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._stream.flush()
        else:
            self.events.append(entry)

    # -- runner hook callbacks (same signatures as Hooks fields) -- #

    async def on_run_start(self, agent: Agent) -> None:
        self._emit(
            "run_start",
            agent=agent.name,
            model=agent.model,
            tools=[t.name for t in agent.tools.all()],
        )

    async def on_turn_start(self, turn: int, agent: Agent) -> None:
        self._emit("turn_start", turn=turn)

    async def on_model_call(self, agent: Agent) -> None:
        self._emit("model_call", tools_available=len(agent.tools.all()))

    async def on_text(self, text: str) -> None:
        self._emit("text", text=_trim(text, self._max))

    async def on_reasoning(self, text: str) -> None:
        self._emit("reasoning", text=_trim(text, self._max))

    async def on_tool_call(self, tool_call: ToolCall, agent: Agent | None) -> None:
        self._emit(
            "tool_call",
            id=tool_call.id,
            name=tool_call.name,
            arguments=_trim(tool_call.arguments, self._max),
        )

    async def on_tool_result(
        self, tool_call: ToolCall, result: ToolResult, agent: Agent | None
    ) -> None:
        self._emit(
            "tool_result",
            id=tool_call.id,
            name=tool_call.name,
            is_error=result.is_error,
            content=_trim(result.content, self._max),
        )

    async def on_final(self, result: RunResult) -> None:
        self._emit(
            "run_end",
            session_id=result.session_id,
            turns=result.turns,
            final=_trim(result.final_output, self._max),
        )

    # -- assembly -- #

    def make_hooks(self) -> Hooks:
        """Build a :class:`Hooks` wired to this tracer."""
        return Hooks(
            on_run_start=self.on_run_start,
            on_turn_start=self.on_turn_start,
            on_model_call=self.on_model_call,
            on_text=self.on_text,
            on_reasoning=self.on_reasoning,
            on_tool_call=self.on_tool_call,
            on_tool_result=self.on_tool_result,
            on_final=self.on_final,
        )

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
