"""End-to-end: a real model calls a tool exposed by an external MCP server.

Connects the fixture stdio MCP server (tests/fixtures/mcp_server.py), registers
its tools on the agent, then asks the model to use one. Verifies the MCP tool
was invoked and the final answer reflects the result.

Run with a configured API key::

    uv run python scripts/e2e_mcp.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import RunDone, Runner, ToolResultEvent
from harness.llm.base import StreamToolCall
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.tools.builtin import builtin_registry
from harness.tools.mcp.client import MCPClientManager, MCPServerConfig
from harness.tools.mcp.manager import register_mcp_server

INSTRUCTIONS = """\
You are a capable AI assistant inside the 'harness' agent framework.
You have tools to call; use them when they help answer the user's question.
Be concise. When you use a tool, report its actual result.
"""

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "mcp_server.py"

PROMPT = (
    "用 mcp_demo_add 工具计算 123 + 77，并告诉我结果。"
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
        name="e2e-mcp",
        instructions=INSTRUCTIONS,
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )

    async with MCPClientManager() as mcp:
        config = MCPServerConfig(
            name="demo", transport="stdio", command=sys.executable, args=[str(FIXTURE)]
        )
        names = await register_mcp_server(mcp, config, agent.tools)
        print(f"MCP server 'demo' registered: {', '.join(names)}")

        runner = Runner(get_provider(settings), session_store=store.sessions)
        tool_used = False
        final_output = ""
        async for event in runner.run_streamed(agent, PROMPT, session_id=session.id):
            if isinstance(event, StreamToolCall) and event.tool_call:
                tool_used = True
                print(f"  ▶ tool call: {event.tool_call.name}")
            elif isinstance(event, ToolResultEvent):
                print(f"  ◀ result: {event.result.content}")
            elif isinstance(event, RunDone):
                final_output = event.result.final_output or ""

    await store.close()

    if not tool_used:
        print("=== E2E MCP FAILED: model never called the MCP tool ===")
        return 1
    print(f"\nfinal: {final_output[:300]}")
    if "200" not in final_output:
        print("=== E2E MCP FAILED: final answer does not reflect the tool result ===")
        return 1
    print("=== E2E MCP PASSED: model called an MCP-exposed tool ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
