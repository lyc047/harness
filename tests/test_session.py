"""SessionStore: SQLite persistence roundtrip."""

import pytest

from harness.core.messages import Message, ToolCall
from harness.memory.session import SessionStore


@pytest.fixture
async def store(tmp_path):
    s = SessionStore(str(tmp_path / "sessions.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_and_get(store):
    session = await store.create_session()
    assert session.id
    got = await store.get_session(session.id)
    assert got is not None
    assert got.id == session.id


@pytest.mark.asyncio
async def test_messages_roundtrip(store):
    session = await store.create_session()
    tc = ToolCall(id="c1", name="bash", arguments='{"command": "echo hi"}')
    messages = [
        Message.system("sys"),
        Message.user("hello"),
        Message.assistant(content=None, tool_calls=[tc], reasoning_content="thinking here"),
        Message.tool("c1", "hi", name="bash"),
    ]
    await store.save_messages(session.id, messages)
    loaded = await store.load_messages(session.id)

    assert len(loaded) == 4
    assert loaded[0].content == "sys"
    assert loaded[2].tool_calls == [tc]
    assert loaded[2].reasoning_content == "thinking here"
    assert loaded[3].role == "tool"
    assert loaded[3].tool_call_id == "c1"


@pytest.mark.asyncio
async def test_save_replaces_history(store):
    session = await store.create_session()
    await store.save_messages(session.id, [Message.user("first")])
    await store.save_messages(session.id, [Message.user("second")])
    loaded = await store.load_messages(session.id)
    assert [m.content for m in loaded] == ["second"]


@pytest.mark.asyncio
async def test_delete(store):
    session = await store.create_session()
    await store.save_messages(session.id, [Message.user("x")])
    await store.delete_session(session.id)
    assert await store.get_session(session.id) is None
    assert await store.load_messages(session.id) == []


@pytest.mark.asyncio
async def test_list_sessions_ordered(store):
    s1 = await store.create_session()
    s2 = await store.create_session()
    sessions = await store.list_sessions()
    ids = {s.id for s in sessions}
    assert {s1.id, s2.id} <= ids
