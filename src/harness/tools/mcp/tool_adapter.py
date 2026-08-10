"""Adapt MCP-discovered tools into local harness :class:`Tool` objects."""

from __future__ import annotations

from typing import Any

from harness.observability.logging import get_logger
from harness.tools.base import Tool, ToolResult
from harness.tools.mcp.client import MCPClientManager

logger = get_logger("tools.mcp")


class MCPToolAdapter(Tool):
    """A harness Tool that delegates invocation to a live MCP server session.

    Named ``mcp_<server>_<tool>`` so MCP tools can never collide with builtins
    (the ``server`` attribute also lets us unregister by server).
    """

    def __init__(
        self,
        *,
        server: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        manager: MCPClientManager,
    ) -> None:
        self.server = server
        self.mcp_tool_name = tool_name
        self._manager = manager
        super().__init__(
            name=f"mcp_{server}_{tool_name}",
            description=description,
            parameters_schema=input_schema,
        )

    async def invoke(self, **kwargs: Any) -> ToolResult:
        try:
            body = await self._manager.call_tool(self.server, self.mcp_tool_name, kwargs)
            return ToolResult.ok(body, server=self.server, tool=self.mcp_tool_name)
        except Exception as exc:  # noqa: BLE001 — surface MCP failures to the model
            logger.warning(
                "mcp tool %s/%s raised %s", self.server, self.mcp_tool_name, exc
            )
            return ToolResult.error(
                f"{type(exc).__name__}: {exc}",
                server=self.server,
                tool=self.mcp_tool_name,
            )
