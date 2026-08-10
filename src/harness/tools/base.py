"""Tool abstraction and the ``@tool`` decorator.

A :class:`Tool` wraps a callable (sync or async) plus a JSON Schema derived
from its type annotations. Tools are the only things the model can call; the
runner resolves a tool call by name against the agent's registry and invokes
the tool, capturing the result (or a caught exception) into a :class:`ToolResult`.
"""

from __future__ import annotations

import inspect
import json
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin, overload

from harness.observability.logging import get_logger

logger = get_logger("tools")

_JSON_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def type_to_schema(tp: Any) -> dict[str, Any]:
    """Convert a Python type annotation into a JSON Schema fragment."""
    if tp is inspect.Parameter.empty or tp is None or tp is Any:
        return {}
    # Optional[T] == Union[T, None]  (covers typing.Union and PEP 604 `T | None`)
    origin = get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(tp) if a is not type(None)]  # noqa: E721
        if len(args) == 1:
            return type_to_schema(args[0])
        return {"anyOf": [type_to_schema(a) for a in args]}
    if origin is list or origin is list:
        item = get_args(tp)
        schema: dict[str, Any] = {"type": "array"}
        if item:
            schema["items"] = type_to_schema(item[0])
        return schema
    if origin is dict or origin is dict:
        return {"type": "object"}
    if tp in _JSON_TYPE_MAP:
        return {"type": _JSON_TYPE_MAP[tp]}
    # Fall back to string for unknown annotations (enums, custom types, ...).
    return {"type": "string"}


@dataclass
class ToolResult:
    """The outcome of running a tool."""

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, **meta: Any) -> ToolResult:
        return cls(content=content, metadata=meta)

    @classmethod
    def error(cls, content: str, **meta: Any) -> ToolResult:
        return cls(content=content, is_error=True, metadata=meta)


class Tool:
    """A callable tool exposed to the model as a JSON Schema."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        func: Callable[..., Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters_schema = parameters_schema
        self._func = func
        self._is_async = inspect.iscoroutinefunction(func) if func else False

    # -- schema -- #
    def to_function_schema(self) -> dict[str, Any]:
        """OpenAI wire-format function schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    # -- invocation -- #
    async def invoke(self, **kwargs: Any) -> ToolResult:
        """Run the tool, capturing results and exceptions into a ToolResult."""
        if self._func is None:
            return ToolResult.error(f"tool {self.name!r} has no implementation")
        try:
            result = await self._func(**kwargs) if self._is_async else self._func(**kwargs)
            if isinstance(result, str):
                return ToolResult.ok(result)
            try:
                return ToolResult.ok(json.dumps(result, ensure_ascii=False, default=str))
            except TypeError:
                return ToolResult.ok(str(result))
        except Exception as exc:  # noqa: BLE001 — surface any tool failure to the model
            logger.warning("tool %r raised %s: %s", self.name, type(exc).__name__, exc)
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Tool {self.name}>"


def _build_parameters_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Derive an object JSON Schema from a function's signature."""
    hints = {}
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # noqa: BLE001 — unresolvable hints degrade gracefully
        hints = {}
    sig = inspect.signature(func)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue  # *args / **kwargs not representable in function-calling
        tp = hints.get(name, Any)
        properties[name] = type_to_schema(tp)
        if param.default is inspect.Parameter.empty:
            required.append(name)
    if not properties:
        properties = {}
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


@overload
def tool(name: Callable[..., Any]) -> Tool: ...
@overload
def tool(
    name: str | None = None,
    description: str | None = None,
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    name: str | Callable[..., Any] | None = None,
    description: str | None = None,
) -> Callable[[Callable[..., Any]], Tool] | Tool:
    """Decorator: turn a function into a :class:`Tool`.

    Usage::

        @tool
        async def read_file(path: str) -> str: ...

        @tool(name="run_bash", description="Run a shell command")
        def bash(command: str) -> str: ...
    """

    def decorator(func: Callable[..., Any]) -> Tool:
        doc = inspect.getdoc(func) or func.__name__
        first_line = doc.split("\n")[0].strip() if doc else func.__name__
        tool_name = name if isinstance(name, str) else func.__name__
        return Tool(
            name=tool_name,
            description=description or first_line,
            parameters_schema=_build_parameters_schema(func),
            func=func,
        )

    if callable(name):  # used bare: @tool
        return decorator(name)
    return decorator
