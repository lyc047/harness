"""Real-model demo: show the parent agent *autonomously* delegating to subagents.

Builds the same core stack the CLI/web use (``build_core_stack`` +
``add_example_subagents``), auto-approves tool calls, and prints a distilled
transcript via hooks. Nested runs (parent → subagent) are indented.

Run:  uv run python scripts/demo_subagents.py ["task text"]
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from harness.config import Settings
from harness.core.compose import add_example_subagents, build_core_stack
from harness.core.hooks import Hooks
from harness.core.messages import ToolCall
from harness.memory.store import Store

SUBAGENTS = {
    "researcher",
    "coder",
    "frontend_design",
    "doc_writer",
    "search",
    "file_handler",
}


async def _auto_approve(_tc: ToolCall) -> str:
    return "y"  # demo only: never prompt, let the run finish unattended


def make_hooks() -> Hooks:
    """Indent nested runs; the parent is the base of the stack."""
    stack: list[str] = []

    async def on_run_start(agent) -> None:
        stack.append(agent.name)
        print(f"{'  ' * (len(stack) - 1)}[RUN] {agent.name}")

    async def on_final(result) -> None:
        name = stack.pop()
        out = (result.final_output or "")[:300].replace("\n", " ")
        print(f"{'  ' * len(stack)}[DONE] {name} -> {out}")

    async def on_tool_call(tc, agent) -> None:
        if agent is None or not stack or stack[-1] != agent.name:
            return  # streaming pre-announce (agent=None) or stale event
        print(f"{'  ' * (len(stack) - 1)}  [TOOL] {agent.name} -> {tc.name}({tc.arguments[:180]})")

    async def on_tool_result(tc, result, agent) -> None:
        if agent is None or result is None:
            return
        snippet = (result.content or "")[:240].replace("\n", " ")
        print(f"{'  ' * (len(stack) - 1)}  [RES]  {agent.name} <- {tc.name}: {snippet}")

    return Hooks(
        on_run_start=on_run_start,
        on_final=on_final,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )


async def main(task: str) -> None:
    # Windows console is GBK; force UTF-8 so Chinese transcripts print cleanly.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    settings = Settings.load().replace(
        db_path=str(Path(tempfile.gettempdir()) / "harness-demo-subagents.db")
    )
    store = Store(settings)
    await store.initialize()
    try:
        stack = await build_core_stack(
            settings, store=store, hooks=make_hooks(), prompt=_auto_approve
        )
        add_example_subagents(stack)

        delegate_tools = sorted(
            n for n in stack.agent.tools.names() if n.startswith("delegate_to_")
        )
        print(f"main agent tools (delegate): {delegate_tools}")
        print(f"\nUSER: {task}\n")

        result = await stack.runner.run(stack.agent, task, session_id=None)
        print(f"\n=== FINAL ANSWER ===\n{result.final_output}")
    finally:
        await store.close()


if __name__ == "__main__":
    task = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "研究这个仓库的权限与审批机制:permissions/approver 代码在哪些文件,"
        "plan/ask/auto/full 四种权限模式的差别。用中文给一份 150 字以内的要点总结,不要写文件。"
    )
    asyncio.run(main(task))
