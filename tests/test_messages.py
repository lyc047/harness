"""Message model: wire format and reasoning_content passthrough."""

from harness.core.messages import Message, ToolCall


def test_user_wire():
    assert Message.user("hi").to_openai_dict() == {"role": "user", "content": "hi"}


def test_tool_call_wire():
    tc = ToolCall(id="call_1", name="read_file", arguments='{"path": "a.txt"}')
    msg = Message.assistant(content=None, tool_calls=[tc])
    d = msg.to_openai_dict()
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "read_file"
    assert d["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'


def test_reasoning_content_passthrough():
    msg = Message.assistant(content="answer", reasoning_content="thinking...")
    d = msg.to_openai_dict()
    assert d["reasoning_content"] == "thinking..."


def test_roundtrip():
    tc = ToolCall(id="c1", name="bash", arguments="{}")
    original = Message.assistant(
        content=None, tool_calls=[tc], reasoning_content="should I run bash?"
    )
    restored = Message.from_openai_dict(original.to_openai_dict())
    assert restored.role == "assistant"
    assert restored.tool_calls == [tc]
    assert restored.reasoning_content == "should I run bash?"


def test_tool_message_wire():
    msg = Message.tool("c1", "output", name="bash")
    d = msg.to_openai_dict()
    assert d["role"] == "tool"
    assert d["tool_call_id"] == "c1"
    assert d["name"] == "bash"
    assert d["content"] == "output"


def test_arguments_dict_parsing():
    tc = ToolCall(id="x", name="t", arguments='{"a": 1, "b": [1,2]}')
    assert tc.arguments_dict == {"a": 1, "b": [1, 2]}
    assert ToolCall(id="x", name="t", arguments="not-json").arguments_dict == {}


def test_without_reasoning():
    msg = Message.assistant("a", reasoning_content="r")
    stripped = msg.without_reasoning()
    assert stripped.reasoning_content is None
    assert stripped.content == "a"
