"""OpenAICompatProvider: streaming accumulation, thinking mode, response parsing.

We inject a fake AsyncOpenAI client so no network is involved.
"""

from types import SimpleNamespace

import pytest

from harness.core.messages import Message
from harness.llm.base import StreamEnd, StreamReasoning, StreamText, StreamToolCall
from harness.llm.openai_compat import OpenAICompatProvider


def _provider(fake_completions) -> OpenAICompatProvider:
    p = OpenAICompatProvider(model="deepseek-v4-flash", api_key="sk-test", retry_attempts=1)
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
        self.plain_message = None

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            return _AsyncIterable(self.chunks or [])
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
