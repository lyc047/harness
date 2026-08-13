"""OpenAI-compatible chat-completions provider (used for DeepSeek).

Key DeepSeek specifics handled here:
- ``base_url = https://api.deepseek.com``
- thinking mode: assistant messages carry ``reasoning_content`` which must be
  passed back verbatim on subsequent calls (else 400 error). We preserve it in
  :class:`~harness.core.messages.Message` and re-emit it on the wire.
- parallel tool calls (up to ``max_tool_calls``).
- transient errors (timeouts, 429, 5xx) are retried with exponential backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from harness.core.messages import Message, ToolCall
from harness.llm.base import (
    LLMResponse,
    StreamEnd,
    StreamEvent,
    StreamReasoning,
    StreamText,
    StreamToolCall,
    ToolSchema,
)
from harness.observability.logging import get_logger

logger = get_logger("llm.openai_compat")

# Transient errors worth retrying with backoff.
_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
)


class OpenAICompatProvider:
    """Chat-completions provider talking to any OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
        max_tool_calls: int = 128,
        retry_attempts: int = 3,
        retry_base_delay: float = 1.0,
        track_usage: bool = False,
    ) -> None:
        self.model = model
        self.max_tool_calls = max_tool_calls
        self.retry_attempts = max(1, retry_attempts)
        self.retry_base_delay = retry_base_delay
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self.track_usage = track_usage
        self.usage_log: list[dict[str, Any]] = []
        # The client is built lazily: newer openai versions raise
        # OpenAIError("Missing credentials") at construction, which would crash
        # the web server (which must boot without a key and surface the error
        # inside a run). Tests also overwrite ``_client`` with a fake.
        self._client: AsyncOpenAI | None = None

    @property
    def _openai(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._api_key:
                raise RuntimeError(
                    "no API key configured; set DEEPSEEK_API_KEY in .env"
                )
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    # -- public Protocol surface --

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        wire = [m.to_openai_dict() for m in messages]
        resp = await self._request(wire, tools=tools, model=model)
        self._record_usage(model or self.model, getattr(resp, "usage", None))
        return self._parse_message(resp)

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        wire = [m.to_openai_dict() for m in messages]
        stream = await self._request(wire, tools=tools, stream=True, model=model)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_out: list[ToolCall] = []
        # Accumulate streaming tool-call deltas keyed by delta index.
        acc: dict[int, dict[str, str]] = {}

        usage_recorded = False
        async for chunk in self._iter_stream(stream):
            usage = getattr(chunk, "usage", None)
            if not usage_recorded and usage is not None:
                self._record_usage(model or self.model, usage)
                usage_recorded = True
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                yield StreamReasoning(text=reasoning)

            if delta.content:
                text_parts.append(delta.content)
                yield StreamText(text=delta.content)

            for tc in delta.tool_calls or []:
                slot = acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] += tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

            # Emit a tool call as soon as its streamed chunks complete.
            finish_reason = chunk.choices[0].finish_reason
            if finish_reason in {"tool_calls", "stop"}:
                if acc:
                    for slot in acc.values():
                        tool_call = ToolCall(
                            id=slot["id"],
                            name=slot["name"],
                            arguments=slot["arguments"],
                        )
                        tool_calls_out.append(tool_call)
                        yield StreamToolCall(tool_call=tool_call)
                    acc = {}
                if finish_reason == "tool_calls":
                    break

        response = LLMResponse(
            final_text="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls_out or None,
            reasoning_content="".join(reasoning_parts) or None,
        )
        yield StreamEnd(response=response)

    # -- internals --

    async def _iter_stream(self, stream: Any) -> AsyncIterator[Any]:
        """Yield chunks, aborting a read that stalls past ``self._timeout``.

        The SDK's ``timeout`` covers connection + request write/read *setup*,
        but a stream that accepts the request and then goes silent (the
        benchmark's CLOSE_WAIT hang) blocks ``__anext__`` indefinitely. Wrap
        each read so a stalled chunk fails fast as ``openai.APITimeoutError``
        (already in :data:`_RETRYABLE`) instead of hanging the whole run.
        """
        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(), timeout=self._timeout
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise openai.APITimeoutError(
                    request=httpx.Request("POST", self._base_url)
                ) from exc
            yield chunk

    async def _request(
        self,
        wire: list[dict[str, Any]],
        *,
        tools: list[ToolSchema] | None = None,
        stream: bool = False,
        model: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model or self.model, "messages": wire, "stream": stream}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        last_exc: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                return await self._openai.chat.completions.create(**kwargs)
            except _RETRYABLE as exc:
                last_exc = exc
                delay = self.retry_base_delay * (2 ** attempt)
                logger.warning(
                    "transient LLM error (%s), retrying in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
        raise last_exc or RuntimeError("LLM request failed")

    def _parse_message(self, resp: Any) -> LLMResponse:
        message = resp.choices[0].message
        reasoning = getattr(message, "reasoning_content", None)

        tool_calls = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                fn = tc.function
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=fn.name or "",
                        arguments=fn.arguments or "{}",
                    )
                )

        return LLMResponse(
            final_text=message.content,
            tool_calls=tool_calls or None,
            reasoning_content=reasoning or None,
        )

    def _record_usage(self, model: str, usage: Any) -> None:
        """Append one usage record; no-op unless ``track_usage`` is on."""
        if not self.track_usage or usage is None:
            return
        details = getattr(usage, "completion_tokens_details", None)
        self.usage_log.append(
            {
                "model": model,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
            }
        )
