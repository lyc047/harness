"""User-preference persistence.

Preferences are key/value pairs stored in SQLite so they survive across
sessions — e.g. "language = zh", "code_style = black". A :class:`PreferenceStore`
keeps its own connection to the shared database (WAL allows multiple readers
plus a writer in the same process).

The :func:`make_remember_preference_tool` factory exposes ``remember_preference``
to the model so it can record preferences the user states in conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from harness.observability.logging import get_logger
from harness.tools.base import Tool, tool

logger = get_logger("memory.preferences")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PreferenceStore:
    """Persist and load user preferences to the shared SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.executescript(_SCHEMA)
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("PreferenceStore not initialized; call initialize() first")
        return self._conn

    async def get(self, key: str) -> str | None:
        cur = await self._db.execute("SELECT value FROM preferences WHERE key = ?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row is not None else None

    async def get_all(self) -> dict[str, str]:
        cur = await self._db.execute("SELECT key, value FROM preferences")
        rows = await cur.fetchall()
        return {r["key"]: str(r["value"]) for r in rows}

    async def set(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _now()),
        )
        await self._db.commit()

    async def delete(self, key: str) -> None:
        await self._db.execute("DELETE FROM preferences WHERE key = ?", (key,))
        await self._db.commit()


def make_remember_preference_tool(store: PreferenceStore) -> Tool:
    """A tool the agent calls to persist a stated user preference."""

    @tool(
        name="remember_preference",
        description=(
            "Persist a user preference (a key/value fact the user stated, e.g. "
            "'language = zh', 'verbosity = concise'). Kept across sessions. "
            "Call when the user expresses a durable preference."
        ),
    )
    async def remember_preference(key: str, value: str) -> str:
        await store.set(key, value)
        return f"Saved preference {key!r} = {value!r}."

    return remember_preference


__all__ = ["PreferenceStore", "make_remember_preference_tool"]
