"""Runner: the agent turn loop (stateless executor).

Drives an :class:`Agent` through the tool loop:

    user input -> model call -> tool calls -> execute tools -> feed results back -> ...

until the model produces a final answer or ``max_turns`` is exceeded. Mirrors
openai-agents-python's ``_run_impl`` while exposing a streaming event stream
that the CLI renders and other modules hook into.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from harness.context.compactor import ContextCompactor
from harness.context.offload import OffloadExecutor
from harness.core.agent import Agent
from harness.core.hooks import Hooks
from harness.core.messages import Message, ToolCall
from harness.core.run_result import (
    MaxTurnsExceeded,
    RunPaused,
    RunResult,
    RunState,
)
from harness.llm.base import LLMProvider, StreamEnd, StreamEvent
from harness.memory.session import SessionStore
from harness.observability.logging import get_logger
from harness.tools.base import ToolResult

logger = get_logger("core.runner")

# Executes a single tool call -> result. P1 default resolves against the agent's
# registry; P5 wraps it with approval, P7 with sandbox delegation.
ToolExecutor = Callable[[Agent, ToolCall], Awaitable[ToolResult]]

# Consulted at each turn boundary; returning True pauses the run and raises
# RunPaused with a checkpoint for later resume.
PauseCheck = Callable[[RunState], bool]


@dataclass
class RunDone:
    """Terminal event carrying the run result (yielded last)."""

    result: RunResult


@dataclass
class ToolResultEvent:
    """Pairs a tool call with its result, for CLI rendering."""

    tool_call: ToolCall
    result: ToolResult


@dataclass
class CompactionEvent:
    """Yielded after a turn-boundary compaction (CLI/web render a notice)."""

    transcript_path: str
    kept: int
    freed_tokens: int


async def default_executor(agent: Agent, tool_call: ToolCall) -> ToolResult:
    """Resolve a tool call against the agent's registry and run it."""
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
        pause_check: PauseCheck | None = None,
        offload_processor: OffloadExecutor | None = None,
        compactor: ContextCompactor | None = None,
    ) -> None:
        self._provider = provider
        self._hooks = hooks or Hooks()
        self._session_store = session_store
        self._tool_executor = tool_executor or default_executor
        self._pause_check = pause_check
        self._offload_processor = offload_processor
        self._compactor = compactor

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
        concurrent: bool = False,
        provider: LLMProvider | None = None,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | CompactionEvent | RunDone]:
        """Stream events of a full run; a final :class:`RunDone` ends the stream.

        ``concurrent`` runs the tool calls of each multi-call turn in parallel
        (results preserved in call order; a failing call becomes an error
        result and does not abort its siblings). Default False = sequential.
        ``provider`` overrides the Runner's provider for this run only — the
        per-run model tiering seam (subagents may talk to a different backend
        or account than the parent). ``None`` uses the Runner's provider.
        """
        return self._run_streamed(
            agent, user_input, session_id=session_id, concurrent=concurrent, provider=provider
        )

    def resume_streamed(
        self,
        agent: Agent,
        state: RunState,
        *,
        session_id: str | None = None,
        concurrent: bool = False,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | CompactionEvent | RunDone]:
        """Continue a paused run from its :class:`RunState` checkpoint."""
        return self._run_streamed(
            agent,
            None,
            session_id=session_id or state.session_id,
            resume_state=state,
            concurrent=concurrent,
        )

    # -- implementation -- #

    async def _run_streamed(
        self,
        agent: Agent,
        user_input: str | None,
        *,
        session_id: str | None,
        resume_state: RunState | None = None,
        concurrent: bool = False,
        provider: LLMProvider | None = None,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | CompactionEvent | RunDone]:
        stream_provider = provider or self._provider
        await self._hooks.emit(self._hooks.on_run_start, agent)
        if resume_state is not None:
            messages = list(resume_state.messages)
            start_turn = resume_state.turns
            max_turns = resume_state.max_turns
        else:
            messages = await self._prepare_messages(agent, session_id)
            if user_input is not None:
                messages.append(Message.user(user_input))
            start_turn = 0
            max_turns = agent.max_turns
        await self._persist(session_id, messages)

        # Per-run offload binding: session_id is a run argument, so the
        # processor is wrapped here (it is not a static chain layer).
        executor: ToolExecutor = self._tool_executor
        if self._offload_processor is not None:
            inner, offload = executor, self._offload_processor

            async def bound_executor(agent: Agent, tc: ToolCall) -> ToolResult:
                return await offload.process(session_id, tc, await inner(agent, tc))

            executor = bound_executor

        tool_schemas = agent.tool_schemas()

        for turn in range(start_turn, max_turns):
            if self._compactor is not None:
                compacted = await self._compactor.maybe_compact(
                    messages, session_id=session_id, turn=turn
                )
                if compacted.changed:
                    messages = compacted.messages
                    transcript_path = compacted.transcript_path or ""
                    await self._persist(session_id, messages)
                    await self._hooks.emit(
                        self._hooks.on_compacted,
                        transcript_path,
                        compacted.kept,
                        compacted.freed_tokens,
                    )
                    yield CompactionEvent(
                        transcript_path, compacted.kept, compacted.freed_tokens
                    )
            await self._hooks.emit(self._hooks.on_turn_start, turn, agent)
            await self._hooks.emit(self._hooks.on_model_call, agent)

            response = None
            # agent.model (if set) overrides the provider's configured model
            # for this run — the per-agent model tiering seam. Empty string
            # means "inherit the provider default". ``stream_provider`` is the
            # Runner's provider or the per-run override (subagent accounts).
            async for event in stream_provider.stream(
                messages, tools=tool_schemas, model=agent.model or None
            ):
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
                if concurrent:
                    # Parallel: fire every on_tool_call first, gather results
                    # (a failing tool must not abort its siblings), then emit
                    # results back in call order so the model sees stable order.
                    for tool_call in response.tool_calls:
                        await self._hooks.emit(self._hooks.on_tool_call, tool_call, agent)
                    gathered = await asyncio.gather(
                        *(executor(agent, tc) for tc in response.tool_calls),
                        return_exceptions=True,
                    )
                    results = [
                        res if isinstance(res, ToolResult)
                        else ToolResult.error(f"{type(res).__name__}: {res}")
                        for res in gathered
                    ]
                else:
                    results = []
                    for tool_call in response.tool_calls:
                        await self._hooks.emit(self._hooks.on_tool_call, tool_call, agent)
                        results.append(await executor(agent, tool_call))

                for tool_call, tool_result in zip(response.tool_calls, results, strict=True):
                    await self._hooks.emit(
                        self._hooks.on_tool_result, tool_call, tool_result, agent
                    )
                    tool_messages.append(
                        Message.tool(tool_call.id, tool_result.content, name=tool_call.name)
                    )
                    yield ToolResultEvent(tool_call, tool_result)

                messages.extend(tool_messages)
                await self._persist(session_id, messages)
                if self._pause_check is not None:
                    checkpoint = RunState(
                        messages=list(messages),
                        turns=turn + 1,
                        max_turns=max_turns,
                        session_id=session_id,
                    )
                    if self._pause_check(checkpoint):
                        raise RunPaused(checkpoint)
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
