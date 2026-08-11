"""Shared test fixtures: a scripted fake LLM provider."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from harness.core.messages import Message
from harness.llm.base import (
    LLMResponse,
    StreamEnd,
    StreamEvent,
    StreamText,
    StreamToolCall,
    ToolSchema,
)


class FakeProvider:
    """Serves a scripted sequence of responses, one per stream() call."""

    def __init__(self, script: list[LLMResponse] | None = None) -> None:
        self.script = script or []
        self.stream_calls: list[int] = []  # message count at each call
        self.models: list[str | None] = []  # model requested at each call

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        events = [e async for e in self.stream(messages, tools=tools, model=model)]
        end = next(e for e in events if isinstance(e, StreamEnd))
        return end.response

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_calls.append(len(messages))
        self.models.append(model)
        response = self.script.pop(0) if self.script else LLMResponse(final_text="(no script)")
        if response.tool_calls:
            for tc in response.tool_calls:
                yield StreamToolCall(tool_call=tc)
        if response.final_text:
            yield StreamText(text=response.final_text)
        yield StreamEnd(response=response)


@pytest.fixture
def make_provider():
    """Factory for FakeProvider instances."""

    def _make(script: list[LLMResponse] | None = None) -> FakeProvider:
        return FakeProvider(script)

    return _make
