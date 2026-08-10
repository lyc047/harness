"""MCP client manager: server lifecycle, tool discovery, invocation.

Owns one :class:`mcp.ClientSession` per configured server, over stdio or
Streamable HTTP transport. Each server gets its own ``AsyncExitStack`` so
servers can be added/removed at runtime independently — required by the
REPL ``/mcp`` commands and by tools that connect/disconnect on demand.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from harness.observability.logging import get_logger

logger = get_logger("tools.mcp")


@dataclass(frozen=True)
class MCPServerConfig:
    """Connection parameters for one MCP server."""

    name: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str = ""  # stdio: executable
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # http: server URL


@dataclass
class _ServerHandle:
    config: MCPServerConfig
    stack: AsyncExitStack
    session: ClientSession


class MCPClientManager:
    """Manages live MCP sessions; tool discovery and invocation surface.

    Usable as an async context manager so enter/exit stay in the same task
    (anyio cancel scopes from the transports are task-local)::

        async with MCPClientManager() as mcp:
            ...
    """

    def __init__(self) -> None:
        self._servers: dict[str, _ServerHandle] = {}

    async def __aenter__(self) -> MCPClientManager:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- lifecycle --

    async def add_server(self, config: MCPServerConfig) -> list[Any]:
        """Connect ``config`` and return its discovered tools (mcp.types.Tool)."""
        if config.name in self._servers:
            raise ValueError(f"MCP server already connected: {config.name!r}")
        stack = AsyncExitStack()
        try:
            if config.transport == "stdio":
                streams = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=config.command,
                            args=config.args,
                            cwd=config.cwd,
                            env=config.env or None,
                        )
                    )
                )
            elif config.transport == "http":
                streams = await stack.enter_async_context(
                    streamable_http_client(config.url)
                )
            else:
                raise ValueError(f"unknown MCP transport: {config.transport!r}")
            read_stream, write_stream = streams
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except Exception:
            await stack.aclose()  # tear down partial connection
            raise
        self._servers[config.name] = _ServerHandle(config, stack, session)
        tools = (await session.list_tools()).tools or []
        logger.info("MCP server %r connected (%d tools)", config.name, len(tools))
        return list(tools)

    async def remove_server(self, name: str) -> None:
        """Disconnect a server, closing its session and transport."""
        handle = self._servers.pop(name, None)
        if handle is None:
            raise KeyError(f"MCP server not connected: {name!r}")
        await handle.stack.aclose()
        logger.info("MCP server %r disconnected", name)

    async def close(self) -> None:
        """Disconnect every server."""
        for handle in self._servers.values():
            await handle.stack.aclose()
        self._servers.clear()

    @property
    def servers(self) -> list[str]:
        return list(self._servers)

    def is_connected(self, name: str) -> bool:
        return name in self._servers

    # -- invocation --

    async def call_tool(self, server: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and flatten its result into readable text."""
        handle = self._servers.get(server)
        if handle is None:
            raise KeyError(f"MCP server not connected: {server!r}")
        result = await handle.session.call_tool(tool_name, arguments)
        return _render_result(result)


def _render_result(result: Any) -> str:
    """Best-effort flattening of an MCP call result into text.

    Handles text content blocks, ``structured_content`` (JSON-serializable),
    and marks error results so the harness can display them distinctly.
    """
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    if not parts:
        parts.append(str(result))
    body = "\n".join(parts)
    if bool(getattr(result, "is_error", False)):
        return f"[mcp error] {body}"
    return body
