"""A minimal stdio MCP server used as a test fixture.

Exposes three tools over stdio:
  add(a, b)   -> a+b
  echo(text)  -> text unchanged
  fail()      -> always an error result

Run standalone: ``python tests/fixtures/mcp_server.py``
"""

from __future__ import annotations

import asyncio

from mcp import types as t
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_TOOLS = [
    t.Tool(
        name="add",
        description="Add two integers.",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    ),
    t.Tool(
        name="echo",
        description="Return the input text unchanged.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ),
    t.Tool(
        name="fail",
        description="Always return an error result.",
        input_schema={"type": "object", "properties": {}},
    ),
]


async def _list_tools(ctx, params=None) -> t.ListToolsResult:  # noqa: ANN001
    return t.ListToolsResult(tools=_TOOLS)


async def _call_tool(ctx, params) -> t.CallToolResult:  # noqa: ANN001
    name = params.name
    args = params.arguments or {}
    if name == "add":
        text = str(args["a"] + args["b"])
        return t.CallToolResult(content=[t.TextContent(type="text", text=text)])
    if name == "echo":
        return t.CallToolResult(content=[t.TextContent(type="text", text=str(args["text"]))])
    if name == "fail":
        return t.CallToolResult(
            content=[t.TextContent(type="text", text="boom")], is_error=True
        )
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=f"unknown tool: {name}")], is_error=True
    )


async def _main() -> None:
    server = Server(
        "mock-fixture",
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(_main())
