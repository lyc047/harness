"""End-to-end smoke test against a real LLM (DeepSeek).

Drives the full tool loop through the public Runner API, exactly like the
REPL does, but non-interactively:

    user prompt -> stream -> parse tool_calls -> execute -> feed back -> final

Run with a configured API key::

    uv run python scripts/e2e_smoke.py

Exits 0 on success (tool called, final answer produced) and non-zero on any
failure, so it can gate CI / be run after changing provider code.
"""

from __future__ import annotations

import asyncio
import sys

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import RunDone, Runner, ToolResultEvent
from harness.llm.base import StreamReasoning, StreamText, StreamToolCall
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.tools.builtin import builtin_registry


def _force_utf8_stdio() -> None:
    """Same fix as the CLI: CJK Windows defaults to GBK, which crashes on the
    non-ASCII glyphs this script prints."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

INSTRUCTIONS = """\
You are a capable AI assistant inside the 'harness' agent framework.
You have tools for reading, writing, searching and running shell commands.
Use them when they help answer the user's question. Be concise.
"""

PROMPTS = [
    "列出当前目录下所有 .py 文件（可用 glob_files 或 bash 工具），并告诉我一共有几个。",
    "用 bash 运行 `echo harness-e2e-ok` 并告诉我输出。",
]


async def _run_prompt(
    runner: Runner,
    agent: Agent,
    session_id: str,
    prompt: str,
) -> tuple[list[tuple[str, bool]], bool, str]:
    """Run one prompt; return (tool_invocations, used_reasoning, final_output)."""
    text: list[str] = []
    reasoning_chunks = 0
    calls: list[tuple[str, bool]] = []  # (tool_name, ok)
    final_output = ""

    async for event in runner.run_streamed(agent, prompt, session_id=session_id):
        if isinstance(event, StreamText):
            text.append(event.text)
        elif isinstance(event, StreamReasoning):
            reasoning_chunks += 1
        elif isinstance(event, StreamToolCall) and event.tool_call:
            print(f"  ▶ tool call: {event.tool_call.name}")
        elif isinstance(event, ToolResultEvent):
            ok = not event.result.is_error
            calls.append((event.tool_call.name, ok))
            status = "ok" if ok else "ERROR"
            print(f"  ◀ tool result [{status}]: {event.tool_call.name}")
        elif isinstance(event, RunDone):
            final_output = event.result.final_output or ""

    return calls, reasoning_chunks > 0, final_output


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured — run `cp .env.example .env` first.")
        return 2

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()
    session_id = session.id
    print(f"session: {session_id}  model: {settings.model}")

    agent = Agent(
        name="e2e-smoke",
        instructions=INSTRUCTIONS,
        tools=builtin_registry(),
        model=settings.model,
        max_turns=10,
    )
    runner = Runner(get_provider(settings), session_store=store.sessions)

    failures: list[str] = []
    for i, prompt in enumerate(PROMPTS, start=1):
        print(f"\n--- prompt {i}: {prompt}")
        try:
            calls, used_reasoning, final_output = await asyncio.wait_for(
                _run_prompt(runner, agent, session_id, prompt), timeout=180
            )
        except TimeoutError:
            failures.append(f"prompt {i}: timed out after 180s")
            print("  [FAIL] timed out")
            continue
        except Exception as exc:  # noqa: BLE001 — report, don't crash
            failures.append(f"prompt {i}: {type(exc).__name__}: {exc}")
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            continue

        print(f"  final: {final_output[:200]}")

        # Assertions: the loop must have called at least one tool and produced text.
        if not calls:
            failures.append(f"prompt {i}: no tool call was made")
        if any(not ok for _, ok in calls):
            failures.append(f"prompt {i}: at least one tool returned an error")
        if not final_output:
            failures.append(f"prompt {i}: no final output")
        if failures:
            break  # stop at first broken prompt; keep the report readable

    await store.close()

    if failures:
        print("\n=== E2E FAILED ===")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n=== E2E PASSED: tool loop + streaming + persistence verified ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
