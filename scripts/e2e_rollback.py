"""End-to-end: auto-title + write_file rollback with the real model.

Drives a fresh session through the real web stack (``build_core_stack`` via
:class:`harness.web.runtime.Runtime`): the agent writes a file, overwrites it,
then we roll back to the first turn and assert the file is restored to its
original content and the history truncated to just that turn.

Run with a configured API key::

    uv run python scripts/e2e_rollback.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from harness.config import Settings
from harness.memory.store import Store
from harness.web.runtime import Runtime

TARGET = Path("rollback_target.txt")
ORIGINAL = "ORIGINAL-CONTENT"
V1 = "VERSION-ONE-CONTENT"
V2 = "VERSION-TWO-CONTENT"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _write_text(path: Path, content: str) -> None:
    """Sync file IO helpers keep the async main() ASYNC240-clean."""
    path.write_text(content, encoding="utf-8")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _drain(rt: Runtime, *, until: str, timeout: float = 300.0) -> list[dict]:
    """Drain frames, auto-approving every tool so the run makes progress."""
    frames: list[dict] = []
    while True:
        raw = await asyncio.wait_for(rt.outbox.get(), timeout=timeout)
        frame = json.loads(raw)
        frames.append(frame)
        if frame["type"] == "approval_required":
            rt.decisions.put_nowait("y")
        if frame["type"] == until:
            return frames


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    _write_text(TARGET, ORIGINAL)

    store = Store(settings)
    await store.initialize()
    rt = Runtime(settings, store)
    try:
        await rt.start()
        # Use a brand-new session so auto-title fires and the rollback targets
        # only this conversation (not leftover history in the store).
        fresh = await store.sessions.create_session()
        await rt.set_session(fresh.id)
        print(f"session: {fresh.id}")

        rt.start_run(
            f"请用 write_file 工具把 {TARGET} 的内容改成 {V1}。"
            "只修改这一个文件,不要读文件、不要用 bash,写完就结束。"
        )
        await _drain(rt, until="run_done")
        assert _read_text(TARGET) == V1, "turn 1 did not write V1"
        print("turn 1: wrote VERSION-ONE-CONTENT ✓")

        rt.start_run(
            f"请再次用 write_file 工具把 {TARGET} 的内容改成 {V2}。"
            "只修改这一个文件,不要读文件、不要用 bash,写完就结束。"
        )
        await _drain(rt, until="run_done")
        assert _read_text(TARGET) == V2, "turn 2 did not write V2"
        print("turn 2: wrote VERSION-TWO-CONTENT ✓")

        # Roll back to the first user message (step 1): both writes undone.
        await rt.rollback(1)
        rollback_frames = await _drain(rt, until="rolled_back")
        rb = rollback_frames[-1]
        print(f"rolled back to idx {rb['to_idx']}; restored: {rb['restored']}")
        assert _read_text(TARGET) == ORIGINAL, "file not restored"
        messages = await store.sessions.load_messages(fresh.id)
        roles = [m.role for m in messages]
        assert roles == ["system", "user"], f"history not truncated: {roles}"
        print(f"history after rollback: {roles} ✓")
    finally:
        await rt.shutdown()
        await store.close()
        _unlink(TARGET)

    print("=== E2E ROLLBACK PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
