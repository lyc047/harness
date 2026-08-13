"""OpenAICompatProvider: streaming accumulation, thinking mode, response parsing.

We inject a fake AsyncOpenAI client so no network is involved.
"""

import asyncio
from types import SimpleNamespace

import openai
import pytest

from harness.core.messages import Message
from harness.llm.base import StreamEnd, StreamReasoning, StreamText, StreamToolCall
from harness.llm.openai_compat import OpenAICompatProvider


def _provider(
    fake_completions, *, track_usage=False, timeout=60.0
) -> OpenAICompatProvider:
    p = OpenAICompatProvider(
        model="deepseek-v4-flash",
        api_key="sk-test",
        retry_attempts=1,
        track_usage=track_usage,
        timeout=timeout,
    )
    # Provider calls self._client.chat.completions.create(...) — mirror that chain.
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    return p


def _chunk(*, content=None, reasoning=None, tool_deltas=None, finish_reason=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_deltas,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _tc(index, id_=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id_, function=fn)


class FakeCompletions:
    """Simulates chat.completions.create for both streamed and plain calls."""

    def __init__(self):
        self.chunks: list | None = None
        self.stream_result = None  # overrides chunks when set (e.g. a stalled stream)
        self.plain_message = None

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            return self.stream_result or _AsyncIterable(self.chunks or [])
        return SimpleNamespace(choices=[SimpleNamespace(message=self.plain_message)])


class _AsyncIterable:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        self._it = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _StalledStream:
    """A stream whose reads never complete within the provider's timeout."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(30)  # far longer than any test timeout
        raise StopAsyncIteration from None


@pytest.mark.asyncio
async def test_complete_parses_tool_calls_and_reasoning():
    fake = FakeCompletions()
    fake.plain_message = SimpleNamespace(
        content=None,
        reasoning_content="let me think",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="bash", arguments='{"command": "ls"}'),
            )
        ],
    )
    provider = _provider(fake)

    resp = await provider.complete([Message.user("hi")], tools=None)
    assert resp.final_text is None
    assert resp.reasoning_content == "let me think"
    assert resp.tool_calls is not None
    assert resp.tool_calls[0].name == "bash"
    assert resp.tool_calls[0].arguments_dict == {"command": "ls"}


@pytest.mark.asyncio
async def test_stream_accumulates_text_reasoning_and_tool_calls():
    fake = FakeCompletions()
    fake.chunks = [
        _chunk(reasoning="think", finish_reason=None),
        _chunk(content="hello", finish_reason=None),
        _chunk(
            tool_deltas=[
                _tc(0, id_="call_", name="read_", arguments='{"p'),
            ],
            finish_reason=None,
        ),
        _chunk(
            tool_deltas=[
                _tc(0, id_="1", name="file", arguments='ath": "a.txt"}'),
            ],
            finish_reason="tool_calls",
        ),
    ]
    provider = _provider(fake)

    events = [e async for e in provider.stream([Message.user("hi")], tools=None)]
    kinds = [type(e) for e in events]
    assert StreamReasoning in kinds
    assert StreamText in kinds
    assert StreamToolCall in kinds
    assert isinstance(events[-1], StreamEnd)

    end = events[-1]
    assert end.response.reasoning_content == "think"
    assert end.response.final_text == "hello"
    assert end.response.tool_calls is not None
    tc = end.response.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments_dict == {"path": "a.txt"}


@pytest.mark.asyncio
async def test_stream_plain_final_only():
    fake = FakeCompletions()
    fake.chunks = [
        _chunk(content="the answer", finish_reason=None),
        _chunk(content=" is 42", finish_reason="stop"),
    ]
    provider = _provider(fake)

    events = [e async for e in provider.stream([Message.user("q")])]
    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.response.final_text == "the answer is 42"
    assert end.response.tool_calls is None


@pytest.mark.asyncio
async def test_request_model_override():
    """A per-call ``model`` overrides the provider's configured model — the seam
    the cheaper subagent model tier relies on (previously agent.model was
    cosmetic and the provider always used its own fixed model)."""
    fake = FakeCompletions()
    fake.plain_message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
    provider = _provider(fake)
    seen: list[str | None] = []

    async def create(**kwargs):
        seen.append(kwargs.get("model"))
        return await FakeCompletions.create(fake, **kwargs)

    fake.create = create
    await provider.complete([Message.user("hi")], model="cheap-model")
    await provider.complete([Message.user("hi")])
    assert seen == ["cheap-model", "deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_messages_include_reasoning_passthrough():
    """Assistant reasoning_content survives the wire conversion (DeepSeek 400 guard)."""
    from harness.core.messages import Message

    msg = Message.assistant(content="done", reasoning_content="reasoning-to-keep")
    d = msg.to_openai_dict()
    assert d["reasoning_content"] == "reasoning-to-keep"


def _usage(prompt_tokens, completion_tokens, reasoning_tokens=0):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


@pytest.mark.asyncio
async def test_complete_records_usage_when_tracking():
    fake = FakeCompletions()
    fake.plain_message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=fake.plain_message)],
            usage=_usage(10, 5, 2),
        )

    fake.create = create
    provider = _provider(fake, track_usage=True)
    await provider.complete([Message.user("hi")])
    assert provider.usage_log == [
        {
            "model": "deepseek-v4-flash",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "reasoning_tokens": 2,
        }
    ]


@pytest.mark.asyncio
async def test_stream_records_usage_once_from_final_chunk():
    fake = FakeCompletions()
    c1 = _chunk(content="hi", finish_reason=None)
    c1.usage = _usage(10, 5, 2)
    c2 = _chunk(content=" there", finish_reason="stop")
    c2.usage = _usage(10, 6, 3)  # should NOT be double counted
    fake.chunks = [c1, c2]
    provider = _provider(fake, track_usage=True)
    events = [e async for e in provider.stream([Message.user("hi")])]
    assert isinstance(events[-1], StreamEnd)
    assert len(provider.usage_log) == 1
    assert provider.usage_log[0]["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_usage_tracking_off_by_default():
    fake = FakeCompletions()
    fake.plain_message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
    provider = _provider(fake)  # track_usage default False
    await provider.complete([Message.user("hi")])
    assert provider.usage_log == []


@pytest.mark.asyncio
async def test_stream_stalled_read_raises_api_timeout():
    """A stream whose chunks stop arriving must fail fast with a retryable
    APITimeoutError — the SDK's connection timeout does not cover reads once
    a stream is open (the CLOSE_WAIT hang in the token-economy benchmark)."""
    fake = FakeCompletions()
    fake.stream_result = _StalledStream()
    provider = _provider(fake, timeout=0.1)

    with pytest.raises(openai.APITimeoutError):
        async for _ in provider.stream([Message.user("hi")]):
            pass
    assert provider.usage_log == []
