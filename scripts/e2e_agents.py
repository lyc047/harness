"""End-to-end: a parent agent delegates subtasks to subagents (manager pattern).

Enables the built-in researcher/coder subagents, asks the model to decompose
a task, and verifies it actually called delegate_to_* and produced a final
answer reflecting the subagents' work.

Run with a configured API key::

    uv run python scripts/e2e_agents.py
"""

from __future__ import annotations

import asyncio
import sys

from harness.agents.examples import example_subagents
from harness.agents.orchestrator import add_subagents
from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import RunDone, Runner, ToolResultEvent
from harness.llm.base import StreamToolCall
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.tools.builtin import builtin_registry

INSTRUCTIONS = """\
You are a manager agent. You have delegation tools (delegate_to_researcher,
delegate_to_coder). When a task has distinct research or coding parts, delegate
them to the right subagent instead of doing everything yourself, then combine
their results into a concise final answer. Always report what was delegated.
"""

PROMPT = (
    "用 delegate_to_researcher 调研 src/harness 目录下有哪些模块，"
    "再用 delegate_to_coder 创建文件 p3_out.txt 内容为 'p3-ok'，然后汇总结果。"
)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    agent = Agent(
        name="manager",
        instructions=INSTRUCTIONS,
        tools=builtin_registry(),
        model=settings.model,
        max_turns=12,
    )
    runner = Runner(get_provider(settings), session_store=store.sessions)
    add_subagents(agent, runner, example_subagents())
    print(f"delegation tools: {[t for t in agent.tools.names() if t.startswith('delegate_')]}")

    delegated: set[str] = set()
    results: list[str] = []
    final_output = ""

    async def _collect() -> None:
        nonlocal final_output
        async for event in runner.run_streamed(agent, PROMPT, session_id=session.id):
            if isinstance(event, StreamToolCall) and event.tool_call:
                name = event.tool_call.name
                if name.startswith("delegate_to_"):
                    delegated.add(name)
                    print(f"  ▶ delegate: {name}")
            elif isinstance(event, ToolResultEvent):
                if event.tool_call.name.startswith("delegate_to_"):
                    snippet = event.result.content[:120]
                    results.append(snippet)
                    print(f"  ◀ delegated result: {snippet!r}")
            elif isinstance(event, RunDone):
                final_output = event.result.final_output or ""

    try:
        await asyncio.wait_for(_collect(), timeout=360)
    except TimeoutError:
        await store.close()
        print("=== E2E AGENTS FAILED: timed out after 360s ===")
        return 1

    await store.close()

    if not delegated:
        print("=== E2E AGENTS FAILED: parent never delegated to a subagent ===")
        return 1
    if not final_output:
        print("=== E2E AGENTS FAILED: no final answer ===")
        return 1
    print(f"\nfinal: {final_output[:300]}")
    print(f"=== E2E AGENTS PASSED: delegated to {sorted(delegated)} ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
