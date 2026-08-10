"""ToolRegistry: named collection of tools with schema export."""

from __future__ import annotations

from typing import Any

from harness.tools.base import Tool


class ToolRegistry:
    """Holds tools by name; produces OpenAI function schemas for the provider."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for t in tools:
            self.register(t)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name!r}")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def to_function_schemas(self) -> list[dict[str, Any]]:
        """Schemas in OpenAI wire format for the ``tools`` request parameter."""
        return [t.to_function_schema() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)
