"""Bridge between the MCP client manager and a local ToolRegistry."""

from __future__ import annotations

from harness.observability.logging import get_logger
from harness.tools.mcp.client import MCPClientManager, MCPServerConfig
from harness.tools.mcp.tool_adapter import MCPToolAdapter
from harness.tools.registry import ToolRegistry

logger = get_logger("tools.mcp")


async def register_mcp_server(
    manager: MCPClientManager,
    config: MCPServerConfig,
    registry: ToolRegistry,
) -> list[str]:
    """Connect an MCP server and register adapted tools on ``registry``.

    Returns the local tool names registered (e.g. for ``/mcp list`` output).
    """
    tools = await manager.add_server(config)
    names: list[str] = []
    for tool in tools:
        adapter = MCPToolAdapter(
            server=config.name,
            tool_name=tool.name,
            description=tool.description or f"MCP tool {tool.name} from {config.name}",
            input_schema=dict(tool.input_schema or {}),
            manager=manager,
        )
        registry.register(adapter)
        names.append(adapter.name)
    logger.info("registered %d tools from MCP server %r", len(names), config.name)
    return names


def unregister_mcp_server(server: str, registry: ToolRegistry) -> None:
    """Drop every adapted tool belonging to ``server`` from the registry."""
    for tool in list(registry.all()):
        if getattr(tool, "server", None) == server:
            registry.unregister(tool.name)
