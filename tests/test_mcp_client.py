"""Integration tests for the MCP client against a real stdio subprocess.

Each test spawns the fixture server as a stdio subprocess (mcp_server.py)
and exercises discovery, invocation, lifecycle and registry integration.
The manager is used as ``async with`` so transport cleanup happens in the
same task that opened it (anyio cancel scopes are task-local).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness.tools.mcp.client import MCPClientManager, MCPServerConfig
from harness.tools.mcp.manager import register_mcp_server, unregister_mcp_server
from harness.tools.registry import ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"


def _config(**overrides: object) -> MCPServerConfig:
    kwargs: dict[str, object] = {
        "name": "fixture",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE)],
    }
    kwargs.update(overrides)
    return MCPServerConfig(**kwargs)


async def test_discover_tools() -> None:
    async with MCPClientManager() as m:
        tools = await m.add_server(_config())
        assert {"add", "echo", "fail"} <= {t.name for t in tools}


async def test_call_tool_ok() -> None:
    async with MCPClientManager() as m:
        await m.add_server(_config())
        assert await m.call_tool("fixture", "add", {"a": 2, "b": 3}) == "5"
        assert await m.call_tool("fixture", "echo", {"text": "hello"}) == "hello"


async def test_call_tool_error_surface() -> None:
    async with MCPClientManager() as m:
        await m.add_server(_config())
        result = await m.call_tool("fixture", "fail", {})
        assert result.startswith("[mcp error]")


async def test_duplicate_server_rejected() -> None:
    async with MCPClientManager() as m:
        await m.add_server(_config())
        with pytest.raises(ValueError):
            await m.add_server(_config())


async def test_remove_then_unknown() -> None:
    async with MCPClientManager() as m:
        await m.add_server(_config())
        await m.remove_server("fixture")
        assert m.servers == []
        with pytest.raises(KeyError):
            await m.remove_server("fixture")
        with pytest.raises(KeyError):
            await m.call_tool("fixture", "add", {"a": 1, "b": 1})


async def test_unknown_transport_rejected() -> None:
    async with MCPClientManager() as m:
        with pytest.raises(ValueError):
            await m.add_server(_config(transport="carrier-pigeon"))


async def test_registry_integration() -> None:
    """MCP tools register into a ToolRegistry and invoke through the adapter."""
    async with MCPClientManager() as m:
        reg = ToolRegistry()
        names = await register_mcp_server(m, _config(), reg)
        assert set(names) == {"mcp_fixture_add", "mcp_fixture_echo", "mcp_fixture_fail"}

        tool = reg.require("mcp_fixture_add")
        result = await tool.invoke(a=4, b=5)
        assert result.content == "9"
        assert not result.is_error

        # adapter carries its server name so unregister-by-server works
        assert reg.require("mcp_fixture_echo").server == "fixture"

        unregister_mcp_server("fixture", reg)
        assert reg.names() == []
