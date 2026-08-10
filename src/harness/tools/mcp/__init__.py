"""MCP client support: connect to external MCP servers and expose their tools."""

from harness.tools.mcp.client import MCPClientManager, MCPServerConfig
from harness.tools.mcp.tool_adapter import MCPToolAdapter

__all__ = ["MCPClientManager", "MCPServerConfig", "MCPToolAdapter"]
