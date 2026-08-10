"""LLM provider abstraction.

The runner talks only to this Protocol. A provider is responsible for
translating :class:`~harness.core.messages.Message` into its own wire format,
including DeepSeek thinking-mode handling (``reasoning_content`` passthrough).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.core.messages import Message, ToolCall

# A function schema in OpenAI wire format:
#   {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
ToolSchema = dict[str, Any]


@dataclass
class LLMResponse:
    """A parsed model response: either final text, tool calls, or both."""

    final_text: str | None = None
    tool_calls: list[ToolCall] | None = None
    reasoning_content: str | None = None

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


@dataclass
class StreamEvent:
    """Base class for stream events."""

    type: str = ""


@dataclass
class StreamText(StreamEvent):
    """A chunk of visible assistant text."""

    type: str = "text"
    text: str = ""


@dataclass
class StreamReasoning(StreamEvent):
    """A chunk of DeepSeek thinking (reasoning) text."""

    type: str = "reasoning"
    text: str = ""


@dataclass
class StreamToolCall(StreamEvent):
    """A completed tool call (emitted after argument accumulation finishes)."""

    type: str = "tool_call"
    tool_call: ToolCall | None = None


@dataclass
class StreamEnd(StreamEvent):
    """Terminal event carrying the full parsed response."""

    type: str = "end"
    response: LLMResponse = field(default_factory=LLMResponse)


class LLMProvider(Protocol):
    """Contract every model backend must implement.

    ``stream`` is an async *generator* method: calling it returns an
    ``AsyncIterator`` directly (no await), so consumers do
    ``async for event in provider.stream(...)``.
    """

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
    ) -> LLMResponse:
        """Non-streaming call: return the full response."""
        ...

    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming call: yield text/reasoning/tool-call events then StreamEnd."""
        ...
