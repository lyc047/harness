"""Runner: the agent turn loop (stateless executor).

Drives an :class:`Agent` through the tool loop:

    user input -> model call -> tool calls -> execute tools -> feed results back -> ...

until the model produces a final answer or ``max_turns`` is exceeded. Mirrors
openai-agents-python's ``_run_impl`` while exposing a streaming event stream
that the CLI renders and other modules hook into.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from harness.core.agent import Agent
from harness.core.hooks import Hooks
from harness.core.messages import Message, ToolCall
from harness.core.run_result import MaxTurnsExceeded, RunResult
from harness.llm.base import LLMProvider, StreamEnd, StreamEvent
from harness.memory.session import SessionStore
from harness.observability.logging import get_logger
from harness.tools.base import ToolResult

logger = get_logger("core.runner")

# Executes a single tool call -> result. P1 default resolves against the agent's
# registry; P5 wraps it with approval, P7 with sandbox delegation.
ToolExecutor = Callable[[Agent, ToolCall], Awaitable[ToolResult]]


@dataclass
class RunDone:
    """Terminal event carrying the run result (yielded last)."""

    result: RunResult


@dataclass
class ToolResultEvent:
    """Pairs a tool call with its result, for CLI rendering."""

    tool_call: ToolCall
    result: ToolResult


async def _default_executor(agent: Agent, tool_call: ToolCall) -> ToolResult:
    tool = agent.tools.get(tool_call.name)
    if tool is None:
        return ToolResult.error(f"unknown tool: {tool_call.name!r}")
    return await tool.invoke(**tool_call.arguments_dict)


class Runner:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        hooks: Hooks | None = None,
        session_store: SessionStore | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self._provider = provider
        self._hooks = hooks or Hooks()
        self._session_store = session_store
        self._tool_executor = tool_executor or _default_executor

    # -- public API -- #

    async def run(
        self,
        agent: Agent,
        user_input: str,
        *,
        session_id: str | None = None,
    ) -> RunResult:
        """Run to completion and return the result (discards stream events)."""
        result: RunResult | None = None
        async for event in self.run_streamed(agent, user_input, session_id=session_id):
            if isinstance(event, RunDone):
                result = event.result
        if result is None:
            raise RuntimeError("run completed without producing a result")
        return result

    def run_streamed(
        self,
        agent: Agent,
        user_input: str,
        *,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | RunDone]:
        """Stream events of a full run; a final :class:`RunDone` ends the stream."""
        return self._run_streamed(agent, user_input, session_id=session_id)

    # -- implementation -- #

    async def _run_streamed(
        self,
        agent: Agent,
        user_input: str,
        *,
        session_id: str | None,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | RunDone]:
        await self._hooks.emit(self._hooks.on_run_start, agent)
        messages = await self._prepare_messages(agent, session_id)
        messages.append(Message.user(user_input))
        await self._persist(session_id, messages)

        tool_schemas = agent.tool_schemas()

        for turn in range(agent.max_turns):
            await self._hooks.emit(self._hooks.on_turn_start, turn, agent)
            await self._hooks.emit(self._hooks.on_model_call, agent)

            response = None
            async for event in self._provider.stream(messages, tools=tool_schemas):
                if isinstance(event, StreamEnd):
                    response = event.response
                else:
                    await self._emit_stream_hooks(event)
                    yield event

            if response is None:
                raise RuntimeError("provider stream ended without a response")

            if response.tool_calls:
                assistant_msg = Message.assistant(
                    content=response.final_text,
                    tool_calls=response.tool_calls,
                    reasoning_content=response.reasoning_content,
                )
                messages.append(assistant_msg)

                tool_messages: list[Message] = []
                for tool_call in response.tool_calls:
                    await self._hooks.emit(self._hooks.on_tool_call, tool_call, agent)
                    tool_result = await self._tool_executor(agent, tool_call)
                    await self._hooks.emit(
                        self._hooks.on_tool_result, tool_call, tool_result, agent
                    )
                    tool_messages.append(
                        Message.tool(tool_call.id, tool_result.content, name=tool_call.name)
                    )
                    yield ToolResultEvent(tool_call, tool_result)

                messages.extend(tool_messages)
                await self._persist(session_id, messages)
                continue

            # final answer
            messages.append(
                Message.assistant(
                    content=response.final_text or "",
                    reasoning_content=response.reasoning_content,
                )
            )
            await self._persist(session_id, messages)
            result = RunResult(
                final_output=response.final_text,
                messages=list(messages),
                turns=turn + 1,
                session_id=session_id,
            )
            await self._hooks.emit(self._hooks.on_final, result)
            yield RunDone(result)
            return

        raise MaxTurnsExceeded(agent.max_turns)

    # -- helpers -- #

    async def _prepare_messages(self, agent: Agent, session_id: str | None) -> list[Message]:
        """Load persisted history (if any) and ensure the system prompt leads."""
        messages: list[Message] = []
        if self._session_store is not None and session_id:
            messages = await self._session_store.load_messages(session_id)
        if not (messages and messages[0].role == "system"):
            messages.insert(0, Message.system(agent.instructions))
        return messages

    async def _persist(self, session_id: str | None, messages: list[Message]) -> None:
        if self._session_store is not None and session_id:
            await self._session_store.save_messages(session_id, messages)

    async def _emit_stream_hooks(self, event: StreamEvent) -> None:
        from harness.llm.base import StreamReasoning, StreamText, StreamToolCall

        if isinstance(event, StreamText):
            await self._hooks.emit(self._hooks.on_text, event.text)
        elif isinstance(event, StreamReasoning):
            await self._hooks.emit(self._hooks.on_reasoning, event.text)
        elif isinstance(event, StreamToolCall):
            await self._hooks.emit(self._hooks.on_tool_call, event.tool_call, None)
