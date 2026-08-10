"""End-to-end: human-in-the-loop safety features against the real model.

Two scenarios:

1. Approval deny -> model adapts. bash is ASK and a scripted approver always
   denies it; the model must fall back to another tool (write_file) to finish
   the task. We assert the file exists and that bash was blocked at least once.

2. Pause / resume. The runner pauses after the first tool turn (checkpoint),
   then the same run resumes from the checkpoint and completes.

Run with a configured API key::

    uv run python scripts/e2e_safety.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.run_result import RunPaused
from harness.core.runner import RunDone, Runner, default_executor
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.safety.approver import ApprovalExecutor
from harness.safety.permissions import Permission, Permissions, Rule
from harness.tools.builtin import builtin_registry

OUT_FILE = "e2e_safety_out.txt"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _remove_out_file() -> None:
    p = Path(OUT_FILE)
    if p.exists():
        p.unlink()


def _read_out_file() -> str:
    p = Path(OUT_FILE)
    return p.read_text(encoding="utf-8") if p.exists() else ""


async def _scenario_deny_then_adapt(settings: Settings) -> int:
    """bash is denied; the model must switch tools to write the file."""
    _remove_out_file()

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    # Only bash asks; everything else is allowed so the model can fall back.
    perms = Permissions(default=Permission.ALLOW, rules=[Rule("bash", Permission.ASK)])
    bash_denied: list[int] = [0]

    async def prompt(tool_call: object) -> str:
        bash_denied[0] += 1
        return "n"  # always deny bash

    approval = ApprovalExecutor(default_executor, perms, prompt=prompt)  # type: ignore[arg-type]
    runner = Runner(
        get_provider(settings),
        session_store=store.sessions,
        tool_executor=approval,
    )
    agent = Agent(
        name="safety-deny",
        instructions=(
            "If a tool call is blocked or returns an error, adjust your "
            "approach and try another tool to complete the task."
        ),
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )

    prompt_text = (
        f"用 bash 把字符串 'safety-ok' 写入文件 {OUT_FILE}。"
        "bash 被拒绝也没关系，换用其他工具完成。"
    )

    final = ""
    denied_in_history = False
    try:
        async def _run() -> None:
            nonlocal final, denied_in_history
            async for event in runner.run_streamed(agent, prompt_text, session_id=session.id):
                if isinstance(event, RunDone):
                    final = event.result.final_output or ""
                    denied_in_history = any(
                        m.role == "tool"
                        and m.name == "bash"
                        and m.content and "blocked" in m.content
                        for m in event.result.messages
                    )

        await asyncio.wait_for(_run(), timeout=360)
    except TimeoutError:
        await store.close()
        print("=== E2E SAFETY FAILED: scenario 1 timed out ===")
        return 1

    await store.close()

    wrote = "safety-ok" in _read_out_file()
    if not wrote or not denied_in_history or not final:
        print(
            f"=== E2E SAFETY FAILED: wrote={wrote} bash_denied={bash_denied[0]} "
            f"denied_in_history={denied_in_history} final={final[:80]!r}"
        )
        return 1
    print(f"[ok] scenario 1: bash denied {bash_denied[0]}x, model adapted via write_file")
    return 0


async def _scenario_pause_resume(settings: Settings) -> int:
    """A run pauses after the first tool turn, then resumes and completes."""
    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    runner = Runner(
        get_provider(settings),
        session_store=store.sessions,
        pause_check=lambda state: state.turns >= 1,
    )
    agent = Agent(
        name="safety-pause",
        instructions="You are a concise assistant.",
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )

    paused_state = None
    final = ""
    try:
        async def _first() -> None:
            nonlocal paused_state
            try:
                async for _ in runner.run_streamed(
                    agent, "用 bash 运行 `echo hello`，然后告诉我输出", session_id=session.id
                ):
                    pass
            except RunPaused as exc:
                paused_state = exc.state

        async def _second() -> None:
            nonlocal final
            assert paused_state is not None
            async for event in runner.resume_streamed(
                agent, paused_state, session_id=session.id
            ):
                if isinstance(event, RunDone):
                    final = event.result.final_output or ""

        await asyncio.wait_for(_first(), timeout=360)
        if paused_state is None:
            await store.close()
            print("=== E2E SAFETY FAILED: scenario 2 never paused ===")
            return 1
        print(f"[ok] scenario 2: paused at turn {paused_state.turns} with "
              f"{len(paused_state.messages)} messages")
        await asyncio.wait_for(_second(), timeout=360)
    except TimeoutError:
        await store.close()
        print("=== E2E SAFETY FAILED: scenario 2 timed out ===")
        return 1

    await store.close()
    if not final:
        print("=== E2E SAFETY FAILED: scenario 2 no final answer after resume ===")
        return 1
    print(f"[ok] scenario 2: resumed and finished -> {final[:80]!r}")
    return 0


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    if await _scenario_deny_then_adapt(settings) != 0:
        return 1
    if await _scenario_pause_resume(settings) != 0:
        return 1
    print("=== E2E SAFETY PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
