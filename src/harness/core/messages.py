"""Message model shared by the whole framework.

Follows the OpenAI chat-completions wire format so it serialises cleanly
for OpenAI-compatible providers (DeepSeek), with an extra ``reasoning_content``
field used by DeepSeek thinking mode (must be passed back verbatim on
multi-turn tool calls or the API returns 400).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass
class ToolCall:
    """A single tool call emitted by the model."""

    id: str
    name: str
    arguments: str  # JSON-encoded argument object

    @property
    def arguments_dict(self) -> dict[str, Any]:
        """Parse the JSON-encoded arguments (best-effort)."""
        try:
            value = json.loads(self.arguments)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Message:
    """A single message in a conversation."""

    role: str
    content: str | None = None
    # assistant-only: parallel tool calls
    tool_calls: list[ToolCall] | None = None
    # tool-only: which tool call this is the result of
    tool_call_id: str | None = None
    # tool-only: tool name for OpenAI wire format
    name: str | None = None
    # assistant-only: DeepSeek thinking content
    reasoning_content: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=ROLE_SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=ROLE_USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str | None,
        *,
        tool_calls: list[ToolCall] | None = None,
        reasoning_content: str | None = None,
    ) -> Message:
        return cls(
            role=ROLE_ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )

    @classmethod
    def tool(cls, tool_call_id: str, content: str, *, name: str | None = None) -> Message:
        return cls(role=ROLE_TOOL, content=content, tool_call_id=tool_call_id, name=name)

    def without_reasoning(self) -> Message:
        """Return a copy with reasoning stripped (for storage/display)."""
        if not self.reasoning_content:
            return self
        return replace(self, reasoning_content=None)

    # ---- wire-format conversion (OpenAI-compatible) ----

    def to_openai_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        elif self.role in {ROLE_USER, ROLE_SYSTEM}:
            d["content"] = ""  # some providers reject missing content
        if self.role == ROLE_ASSISTANT:
            if self.tool_calls:
                d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
            if self.content is None and not self.tool_calls:
                d["content"] = ""
            if self.reasoning_content:
                d["reasoning_content"] = self.reasoning_content
        if self.role == ROLE_TOOL:
            d["tool_call_id"] = self.tool_call_id
            d["content"] = self.content or ""
            if self.name:
                d["name"] = self.name
        return d

    @classmethod
    def from_openai_dict(cls, d: dict[str, Any]) -> Message:
        role = d.get("role", "")
        content = d.get("content")
        tool_calls = None
        if d.get("tool_calls"):
            tool_calls = []
            for tc in d["tool_calls"]:
                fn = tc.get("function", {})
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=fn.get("name", ""),
                        arguments=fn.get("arguments", "{}"),
                    )
                )
        return cls(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
            reasoning_content=d.get("reasoning_content"),
        )
