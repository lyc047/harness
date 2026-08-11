"""SQLite-backed session store.

Sessions persist the full message history so a conversation can be resumed
after a restart. Uses ``aiosqlite`` and WAL mode to avoid lock contention.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from harness.core.messages import Message, ToolCall
from harness.core.run_result import RunState
from harness.observability.logging import get_logger

logger = get_logger("memory.session")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT,
    parent_session_id TEXT
);
CREATE TABLE IF NOT EXISTS messages (
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
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT,
    data TEXT NOT NULL
);
-- Pre-write snapshots of files the agent overwrites (via write_file). Keyed by
-- the tool_call_id so a rollback can restore every file changed after a point.
-- content is NULL when the file did not exist before the write (existed=0).
CREATE TABLE IF NOT EXISTS file_snapshots (
    tool_call_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    content TEXT,
    existed INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass
class Session:
    id: str
    created_at: str
    updated_at: str
    name: str | None = None
    parent_session_id: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SessionStore:
    """Persist and load sessions/messages to a single SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            # Multiple tabs/writers share one DB file; wait up to 5s instead of
            # failing instantly with "database is locked" during a concurrent write.
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.executescript(_SCHEMA)
            # Existing DBs predate the name/parent columns (there was no schema
            # versioning); add them lazily so old harness.db files upgrade in place.
            for table, column, decl in (
                ("sessions", "name", "TEXT"),
                ("sessions", "parent_session_id", "TEXT"),
            ):
                await self._ensure_column(table, column, decl)
            await self._conn.commit()

    async def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """ALTER TABLE ADD COLUMN if ``column`` is missing from ``table``."""
        cur = await self._db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        names = {r["name"] for r in rows}
        if column not in names:
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            logger.info("migrated %s: added column %s", table, column)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SessionStore not initialized; call initialize() first")
        return self._conn

    async def create_session(
        self, *, name: str | None = None, parent_session_id: str | None = None
    ) -> Session:
        now = _now()
        session = Session(
            id=uuid.uuid4().hex[:12],
            created_at=now,
            updated_at=now,
            name=name,
            parent_session_id=parent_session_id,
        )
        await self._db.execute(
            "INSERT INTO sessions (id, created_at, updated_at, name, parent_session_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (session.id, now, now, name, parent_session_id),
        )
        await self._db.commit()
        return session

    @staticmethod
    def _session_from_row(row: aiosqlite.Row) -> Session:
        return Session(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            name=row["name"],
            parent_session_id=row["parent_session_id"],
        )

    async def get_session(self, session_id: str) -> Session | None:
        cur = await self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return None if row is None else self._session_from_row(row)

    async def list_sessions(self, limit: int = 50) -> list[Session]:
        cur = await self._db.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [self._session_from_row(r) for r in rows]

    async def rename_session(self, session_id: str, name: str) -> bool:
        """Set a session's display name; returns False if the session is unknown."""
        cur = await self._db.execute(
            "UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
            (name or None, _now(), session_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def branch_session(
        self,
        source_id: str,
        *,
        up_to_idx: int,
        name: str | None = None,
    ) -> Session:
        """Fork ``source_id``'s history [0..up_to_idx] into a brand-new session.

        Returns the new session (with ``parent_session_id`` set) and switches
        nothing — the caller decides whether to activate it.
        """
        source = await self.get_session(source_id)
        if source is None:
            raise ValueError(f"unknown session: {source_id}")
        messages = await self.load_messages(source_id)
        keep = [m for i, m in enumerate(messages) if i <= up_to_idx]
        parent_label = source.name or source.id[:8]
        branch = await self.create_session(
            name=name or f"{parent_label} · 分支",
            parent_session_id=source_id,
        )
        await self.save_messages(branch.id, keep)
        return branch

    async def load_messages(self, session_id: str) -> list[Message]:
        cur = await self._db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY idx ASC", (session_id,)
        )
        rows = await cur.fetchall()
        messages: list[Message] = []
        for r in rows:
            raw_tool_calls = json.loads(r["tool_calls"]) if r["tool_calls"] else None
            tool_calls = None
            if raw_tool_calls:
                tool_calls = []
                for tc in raw_tool_calls:
                    fn = tc.get("function", {})
                    tool_calls.append(
                        ToolCall(
                            id=tc.get("id", ""),
                            name=fn.get("name", ""),
                            arguments=fn.get("arguments", "{}"),
                        )
                    )
            messages.append(
                Message(
                    role=r["role"],
                    content=r["content"],
                    tool_calls=tool_calls,
                    tool_call_id=r["tool_call_id"],
                    name=r["name"],
                    reasoning_content=r["reasoning_content"],
                )
            )
        return messages

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Replace the full message history of a session."""
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.executemany(
            """INSERT INTO messages
               (session_id, idx, role, content, tool_calls, tool_call_id, name, reasoning_content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    session_id,
                    i,
                    m.role,
                    m.content,
                    json.dumps([tc.to_dict() for tc in m.tool_calls], ensure_ascii=False)
                    if m.tool_calls
                    else None,
                    m.tool_call_id,
                    m.name,
                    m.reasoning_content,
                )
                for i, m in enumerate(messages)
            ],
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )
        await self._db.commit()

    async def truncate_messages(self, session_id: str, keep_idx: int) -> None:
        """Discard every message with idx > keep_idx (rollback to a point)."""
        await self._db.execute(
            "DELETE FROM messages WHERE session_id = ? AND idx > ?",
            (session_id, keep_idx),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )
        await self._db.commit()

    # -- pre-write file snapshots (rollback of code changes) -- #

    async def save_file_snapshot(
        self,
        tool_call_id: str,
        path: str,
        content: str | None,
        existed: bool,
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO file_snapshots"
            " (tool_call_id, path, content, existed, created_at) VALUES (?, ?, ?, ?, ?)",
            (tool_call_id, path, content, int(existed), _now()),
        )
        await self._db.commit()

    async def load_file_snapshot(self, tool_call_id: str) -> dict[str, Any] | None:
        cur = await self._db.execute(
            "SELECT path, content, existed FROM file_snapshots WHERE tool_call_id = ?",
            (tool_call_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "path": row["path"],
            "content": row["content"],
            "existed": bool(row["existed"]),
        }

    async def delete_session(self, session_id: str) -> None:
        # Remove snapshots for every tool message this session produced, plus
        # its checkpoints, then the session rows themselves.
        cur = await self._db.execute(
            "SELECT tool_call_id FROM messages"
            " WHERE session_id = ? AND role = 'tool' AND tool_call_id IS NOT NULL",
            (session_id,),
        )
        tool_ids = [r["tool_call_id"] for r in await cur.fetchall()]
        if tool_ids:
            await self._db.execute(
                f"DELETE FROM file_snapshots WHERE tool_call_id IN "
                f"({','.join('?' * len(tool_ids))})",
                tool_ids,
            )
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

    # -- run checkpoints (pause/resume) -- #

    async def save_checkpoint(
        self, state: RunState, *, checkpoint_id: str | None = None
    ) -> str:
        """Persist a run checkpoint and return its id."""
        cid = checkpoint_id or uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO checkpoints (id, created_at, session_id, data) VALUES (?, ?, ?, ?)",
            (cid, _now(), state.session_id, state.to_json()),
        )
        await self._db.commit()
        return cid

    async def load_checkpoint(self, checkpoint_id: str) -> RunState | None:
        cur = await self._db.execute(
            "SELECT data FROM checkpoints WHERE id = ?", (checkpoint_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return RunState.from_json(row["data"])

    async def list_checkpoints(self, limit: int = 10) -> list[tuple[str, str]]:
        cur = await self._db.execute(
            "SELECT id, created_at FROM checkpoints ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [(r["id"], r["created_at"]) for r in rows]

    async def delete_checkpoint(self, checkpoint_id: str) -> None:
        await self._db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
        await self._db.commit()
