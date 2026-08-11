"""SessionStore: SQLite persistence roundtrip."""

import sqlite3

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


# -- naming / rollback / branching (three-frontend-feature storage) -- #

@pytest.mark.asyncio
async def test_rename_session(store):
    session = await store.create_session()
    assert await store.rename_session(session.id, "my title") is True
    got = await store.get_session(session.id)
    assert got is not None and got.name == "my title"
    # unknown session -> False
    assert await store.rename_session("nope", "x") is False


@pytest.mark.asyncio
async def test_truncate_messages(store):
    session = await store.create_session()
    await store.save_messages(
        session.id,
        [Message.system("s"), Message.user("a"), Message.assistant("A"),
         Message.user("b"), Message.assistant("B")],
    )
    await store.truncate_messages(session.id, keep_idx=2)
    loaded = await store.load_messages(session.id)
    assert [m.role for m in loaded] == ["system", "user", "assistant"]
    assert [m.content for m in loaded][1:] == ["a", "A"]


@pytest.mark.asyncio
async def test_branch_session_copies_history_and_parent(store):
    source = await store.create_session()
    await store.save_messages(
        source.id,
        [Message.user("q1"), Message.assistant("a1"),
         Message.user("q2"), Message.assistant("a2")],
    )
    branch = await store.branch_session(source.id, up_to_idx=2)
    assert branch.parent_session_id == source.id
    assert branch.name is not None and "分支" in branch.name
    kept = await store.load_messages(branch.id)
    assert [m.content for m in kept] == ["q1", "a1", "q2"]
    # the source session is untouched
    src = await store.load_messages(source.id)
    assert len(src) == 4


@pytest.mark.asyncio
async def test_file_snapshot_roundtrip(store):
    await store.save_file_snapshot("tc1", "a.txt", "old content", True)
    await store.save_file_snapshot("tc2", "b.txt", None, False)
    snap1 = await store.load_file_snapshot("tc1")
    snap2 = await store.load_file_snapshot("tc2")
    assert snap1 == {"path": "a.txt", "content": "old content", "existed": True}
    assert snap2 == {"path": "b.txt", "content": None, "existed": False}
    assert await store.load_file_snapshot("missing") is None


@pytest.mark.asyncio
async def test_delete_session_cleans_snapshots(store):
    session = await store.create_session()
    await store.save_messages(
        session.id,
        [Message.user("q"), Message.tool("tc1", "ok", name="write_file")],
    )
    await store.save_file_snapshot("tc1", "a.txt", "v1", True)
    await store.delete_session(session.id)
    assert await store.load_file_snapshot("tc1") is None


@pytest.mark.asyncio
async def test_migration_adds_name_columns(tmp_path):
    """A pre-existing DB (old schema, no name/parent columns) upgrades in place."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            session_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            name TEXT,
            reasoning_content TEXT,
            PRIMARY KEY (session_id, idx)
        );
        INSERT INTO sessions (id, created_at, updated_at) VALUES ('s1', 'now', 'now');
        """
    )
    conn.commit()
    conn.close()

    store = SessionStore(str(db_path))
    await store.initialize()
    try:
        got = await store.get_session("s1")
        assert got is not None and got.name is None
        # renamed now works on the migrated row
        assert await store.rename_session("s1", "升级后的名字") is True
        assert await store.get_session("s1") is not None
        assert (await store.get_session("s1")).name == "升级后的名字"
    finally:
        await store.close()
