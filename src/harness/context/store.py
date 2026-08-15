"""ContextStore: harness-owned storage for offloaded tool output and
compaction transcripts, plus a token estimator.

All artifacts live under ``<context_dir>/<session_id>/`` on the local
filesystem, kept separate from the agent workspace so they never touch the
sandbox, git, or rollback snapshots.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from uuid import uuid4

from harness.core.messages import Message


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) used for offload/compaction."""
    return len(text) // 4


def estimate_message_tokens(messages: list[Message]) -> int:
    """Token estimate for a whole message list."""
    return sum(estimate_tokens(m.content or "") for m in messages)


_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_filename(token: str) -> str:
    """Whitelist a provider-supplied token for use in a filename."""
    return _SAFE_ID.sub("_", token) or "artifact"


def _safe_session_dir(root: Path, session_id: str) -> Path:
    """Resolve ``<root>/<session_id>``, rejecting names that could escape the root."""
    name = Path(session_id).name
    if name in ("", ".", ".."):
        raise ValueError(f"unsafe session id: {session_id!r}")
    candidate = root / name
    if root.resolve() not in candidate.resolve().parents:
        raise ValueError(f"session dir escapes root: {session_id!r}")
    return candidate


class ContextStore:
    """Filesystem store for compression artifacts, keyed by session id."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def offload(self, session_id: str, tool_call_id: str, content: str) -> Path:
        """Write the full tool output and return the file path."""
        session_dir = _safe_session_dir(self._root, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"offload_{_safe_filename(tool_call_id)}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def write_transcript(
        self, session_id: str, turn: int, messages: list[Message]
    ) -> Path:
        """Write a JSONL transcript of the pre-compaction message history."""
        session_dir = _safe_session_dir(self._root, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"transcript_{turn}_{uuid4().hex[:8]}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for msg in messages:
                fh.write(json.dumps(msg.to_openai_dict(), ensure_ascii=False))
                fh.write("\n")
        return path

    def relpath(self, path: Path) -> str:
        """Path relative to the store root (stable, '/'-separated)."""
        return str(path.relative_to(self._root)).replace("\\", "/")

    def cleanup(self, session_id: str) -> None:
        """Remove every artifact for a session (called on session delete)."""
        try:
            session_dir = _safe_session_dir(self._root, session_id)
        except ValueError:
            return  # unsafe id (e.g. '..'): never delete
        if session_dir.exists():
            shutil.rmtree(session_dir)
