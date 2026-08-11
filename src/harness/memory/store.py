"""Unified storage facade.

Exposes typed sub-stores (sessions, preferences) over a shared SQLite database.
Kept thin so it can grow without touching callers.
"""

from __future__ import annotations

from harness.config import Settings
from harness.memory.preferences import PreferenceStore
from harness.memory.session import SessionStore


class Store:
    """Facade over all persistence concerns of the harness."""

    def __init__(self, settings: Settings) -> None:
        self.sessions = SessionStore(settings.db_path)
        self.preferences = PreferenceStore(settings.db_path)

    async def initialize(self) -> None:
        await self.sessions.initialize()
        await self.preferences.initialize()

    async def close(self) -> None:
        await self.sessions.close()
        await self.preferences.close()
