"""Tool decorator, schema generation, and invocation."""

import pytest

from harness.tools.base import Tool, tool
from harness.tools.registry import ToolRegistry


def test_tool_bare_decorator():
    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    assert isinstance(add, Tool)
    assert add.name == "add"
    assert add.description == "Add two integers."


def test_tool_named_decorator():
    @tool(name="sum_numbers", description="Sum a list")
    def add(numbers: list[int]) -> int:
        return sum(numbers)

    assert add.name == "sum_numbers"
    assert add.description == "Sum a list"


def test_schema_generation():
    @tool
    def complex_op(name: str, count: int = 1, tags: list[str] | None = None) -> str:
        ...

    schema = complex_op.parameters_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["name"]
    assert schema["properties"]["name"] == {"type": "string"}
    assert schema["properties"]["count"] == {"type": "integer"}
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}


def test_function_schema_wire_format():
    @tool
    def ping() -> str:
        """ping"""
        return "pong"

    fs = ping.to_function_schema()
    assert fs["type"] == "function"
    assert fs["function"]["name"] == "ping"
    assert fs["function"]["parameters"]["type"] == "object"


async def test_invoke_sync_and_async():
    @tool
    def sync_fn(x: str) -> str:
        return f"sync:{x}"

    @tool
    async def async_fn(x: str) -> str:
        return f"async:{x}"

    assert (await sync_fn.invoke(x="a")).content == "sync:a"
    assert (await async_fn.invoke(x="b")).content == "async:b"


async def test_invoke_captures_error():
    @tool
    def boom() -> str:
        raise ValueError("kaboom")

    result = await boom.invoke()
    assert result.is_error is True
    assert "kaboom" in result.content


async def test_invoke_non_string_result_json_serialised():
    @tool
    def listing() -> list[str]:
        return ["a", "b"]

    result = await listing.invoke()
    assert result.content == '["a", "b"]'


def test_registry():
    @tool
    def alpha() -> str:
        return "a"

    @tool
    def beta() -> str:
        return "b"

    reg = ToolRegistry()
    reg.register_all([alpha, beta])
    assert set(reg.names()) == {"alpha", "beta"}
    assert len(reg.to_function_schemas()) == 2

    with pytest.raises(ValueError):
        reg.register(alpha)  # duplicate

    reg.unregister("alpha")
    assert reg.get("alpha") is None
    with pytest.raises(KeyError):
        reg.require("alpha")
