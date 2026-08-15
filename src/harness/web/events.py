"""Serialize core stream events and messages to JSON-serializable dicts.

This is the single place that maps the runner/planner event classes to the
WebSocket JSON protocol frames documented in the web UI plan. All functions are
pure (no I/O) so they are trivially unit-testable.
"""

from __future__ import annotations

from typing import Any

from harness.core.messages import Message, ToolCall
from harness.core.runner import CompactionEvent, RunDone, ToolResultEvent
from harness.llm.base import StreamReasoning, StreamText, StreamToolCall
from harness.planning.executor import PlanDone, PlanRevised, StepEnd, StepStart
from harness.planning.models import Plan, PlanStep
from harness.tools.base import ToolResult

_DEFAULT_MAX_TOOL_CHARS = 100_000


def tool_call_to_dict(tool_call: ToolCall) -> dict[str, Any]:
    """Serialize a :class:`ToolCall` to ``{id, name, arguments}``."""
    return {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": tool_call.arguments,
    }


def tool_result_to_dict(
    result: ToolResult, *, max_chars: int = _DEFAULT_MAX_TOOL_CHARS
) -> dict[str, Any]:
    """Serialize a :class:`ToolResult`, truncating long output.

    Multi-megabyte tool output must never blow up a WebSocket frame, so content
    is capped at ``max_chars`` and flagged with ``truncated``.
    """
    content = result.content
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + "\n... (truncated)"
    return {
        "content": content,
        "is_error": result.is_error,
        "truncated": truncated,
        "offloaded": result.metadata.get("offloaded", ""),
    }


def message_to_dict(message: Message) -> dict[str, Any]:
    """Serialize a :class:`Message` to its wire shape for the frontend."""
    d: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        d["content"] = message.content
    if message.tool_calls:
        d["tool_calls"] = [tool_call_to_dict(tc) for tc in message.tool_calls]
    if message.tool_call_id is not None:
        d["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        d["name"] = message.name
    if message.reasoning_content:
        d["reasoning_content"] = message.reasoning_content
    return d


def step_to_dict(step: PlanStep) -> dict[str, Any]:
    """Serialize a :class:`PlanStep` to ``{id, title, description, status}``."""
    return {
        "id": step.id,
        "title": step.title,
        "description": step.description,
        "status": step.status,
    }


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """Serialize a :class:`Plan` to ``{goal, steps: [...]}``."""
    return {"goal": plan.goal, "steps": [step_to_dict(s) for s in plan.steps]}


def serialize_event(event: object) -> dict[str, Any] | None:
    """Map a runner/planner event to a WS frame; ``None`` for unknown events.

    ``StreamEnd`` (the terminal marker with no content of its own) and any
    unhandled event type map to ``None`` and are skipped by the sender.
    """
    if isinstance(event, StreamText):
        return {"type": "text", "text": event.text}
    if isinstance(event, StreamReasoning):
        return {"type": "reasoning", "text": event.text}
    if isinstance(event, StreamToolCall):
        if event.tool_call is None:
            return None
        return {"type": "tool_call", "tool_call": tool_call_to_dict(event.tool_call)}
    if isinstance(event, ToolResultEvent):
        return {
            "type": "tool_result",
            "tool_call_id": event.tool_call.id,
            "name": event.tool_call.name,
            **tool_result_to_dict(event.result),
        }
    if isinstance(event, CompactionEvent):
        return {
            "type": "compacted",
            "transcript": event.transcript_path,
            "kept": event.kept,
            "freed_tokens": event.freed_tokens,
        }
    if isinstance(event, RunDone):
        result = event.result
        return {
            "type": "run_done",
            "result": {
                "final_output": result.final_output,
                "turns": result.turns,
                "session_id": result.session_id,
            },
        }
    if isinstance(event, StepStart):
        return {"type": "step_start", "step": step_to_dict(event.step)}
    if isinstance(event, StepEnd):
        return {"type": "step_end", "step": step_to_dict(event.step), "output": event.output}
    if isinstance(event, PlanRevised):
        return {"type": "plan_revised", "plan": plan_to_dict(event.plan)}
    if isinstance(event, PlanDone):
        return {"type": "plan_done", "plan": plan_to_dict(event.plan)}
    return None


def serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize a message list for the REST history endpoint."""
    return [message_to_dict(m) for m in messages]


__all__ = [
    "tool_call_to_dict",
    "tool_result_to_dict",
    "message_to_dict",
    "step_to_dict",
    "plan_to_dict",
    "serialize_event",
    "serialize_messages",
]
