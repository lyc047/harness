"""MCP tools reach subagents only through explicit mcp_* allowlist entries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from harness.agents.orchestrator import add_subagents, resolve_mcp_tools
from harness.agents.subagent import Subagent
from harness.core.agent import Agent
from harness.core.messages import Message, ToolCall
from harness.core.runner import Runner
from harness.llm.base import (
    LLMResponse,
    StreamEnd,
    StreamEvent,
    StreamText,
    StreamToolCall,
    ToolSchema,
)
from harness.tools.base import Tool, ToolResult
from harness.tools.registry import ToolRegistry


class _FakeTool(Tool):
    def __init__(self, name: str) -> None:
        super().__init__(
            name=name,
            description=f"{name}: fake",
            parameters_schema={"type": "object", "properties": {}},
        )

    async def invoke(self, **kwargs: Any) -> ToolResult:
        return ToolResult.ok(f"{self.name} ran")


def _mcp(server: str, name: str) -> Tool:
    return _FakeTool(f"mcp_{server}_{name}")


def _subagent(name: str, mcp_allowlist: tuple[str, ...] = ()) -> Subagent:
    return Subagent(name=name, instructions=f"{name} instructions", mcp_allowlist=mcp_allowlist)


# ---- allowlist semantics (pure) ---- #


def test_resolve_mcp_tools_allowlist_semantics() -> None:
    parent = ToolRegistry()
    parent.register_all(
        [
            _mcp("demo", "add"),
            _mcp("demo", "list"),
            _mcp("other", "run"),
            _FakeTool("read_file"),  # a non-mcp builtin must never leak
        ]
    )
    # mcp_* matches every mcp tool
    all_sa = _subagent("a", mcp_allowlist=("mcp_*",))
    assert {t.name for t in resolve_mcp_tools(all_sa, parent)} == {
        "mcp_demo_add",
        "mcp_demo_list",
        "mcp_other_run",
    }
    # exact name matches only itself
    one_sa = _subagent("b", mcp_allowlist=("mcp_demo_add",))
    assert [t.name for t in resolve_mcp_tools(one_sa, parent)] == ["mcp_demo_add"]
    # server wildcard matches the whole server
    server_sa = _subagent("c", mcp_allowlist=("mcp_demo_*",))
    assert {t.name for t in resolve_mcp_tools(server_sa, parent)} == {"mcp_demo_add", "mcp_demo_list"}  # noqa: E501
    # default-deny: no allowlist => no MCP tools; a missing parent registry => none
    assert resolve_mcp_tools(_subagent("d"), parent) == []
    assert resolve_mcp_tools(all_sa, None) == []


# ---- integration: a delegated run carries allowlisted MCP tools ---- #


class _ToolsRecordingProvider:
    """Scripted provider that records the tool names of every stream call."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = script
        self.seen: list[list[str]] = []

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.seen.append(sorted(t["function"]["name"] for t in (tools or [])))
        response = self.script.pop(0)
        if response.tool_calls:
            for tc in response.tool_calls:
                yield StreamToolCall(tool_call=tc)
        if response.final_text:
            yield StreamText(text=response.final_text)
        yield StreamEnd(response=response)


def _allow_script() -> list[LLMResponse]:
    return [
        LLMResponse(tool_calls=[_call("p1", "delegate_to_allow", '{"task": "x"}')]),
        LLMResponse(tool_calls=[_call("a1", "mcp_demo_add", '{"a": 1}')]),
        LLMResponse(final_text="allow delivered"),
        LLMResponse(final_text="parent done"),
    ]


def _deny_script() -> list[LLMResponse]:
    return [
        LLMResponse(tool_calls=[_call("p1", "delegate_to_deny", '{"task": "y"}')]),
        LLMResponse(final_text="deny delivered"),
        LLMResponse(final_text="parent done"),
    ]


def _call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


async def test_allowlisted_subagent_sees_mcp_tools() -> None:
    provider = _ToolsRecordingProvider(_allow_script())
    parent = Agent(name="parent", instructions="parent", model="m")
    parent.tools.register(_mcp("demo", "add"))
    runner = Runner(provider)
    add_subagents(parent, runner, [_subagent("allow", mcp_allowlist=("mcp_*",))])

    result = await runner.run(parent, "go", session_id=None)
    assert result.final_output == "parent done"
    # the subagent's own stream call saw the allowlisted MCP tool and called it
    assert "mcp_demo_add" in provider.seen[1]


async def test_unlisted_subagent_never_sees_mcp_tools() -> None:
    provider = _ToolsRecordingProvider(_deny_script())
    parent = Agent(name="parent", instructions="parent", model="m")
    parent.tools.register(_mcp("demo", "add"))
    runner = Runner(provider)
    add_subagents(parent, runner, [_subagent("deny")])

    result = await runner.run(parent, "go", session_id=None)
    assert result.final_output == "parent done"
    # the deny subagent's stream call saw no MCP tools (default deny)
    assert "mcp_demo_add" not in provider.seen[1]
