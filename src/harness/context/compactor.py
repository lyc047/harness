"""ContextCompactor: auto-summarize long histories at the window trigger, and
the on-demand ``compact_conversation`` tool (via a shared CompactRequest flag).

The full pre-compaction history is written to a JSONL transcript under the
ContextStore; the summary message embeds its path so it stays recoverable.
Compaction never blocks a turn: a summary provider failure falls back to a
plain truncation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from harness.context.store import (
    ContextStore,
    estimate_message_tokens,
    estimate_tokens,
)
from harness.core.messages import Message
from harness.llm.base import LLMProvider
from harness.tools.base import Tool, tool

SUMMARY_PROMPT = """\
You are compressing a conversation so it can continue without the full history.
Write a concise structured summary covering: 1) session intent, 2) artifacts or
files produced, 3) key facts and decisions, 4) the next step, 5) open or
unresolved items. The summary will replace the entire history below. Keep it
dense and factual.
"""

_RECENT_TOKEN_FRACTION = 0.1


class CompactRequest:
    """One-shot flag: the compact tool sets it; the compactor consumes it."""

    def __init__(self) -> None:
        self.requested = False

    def set(self) -> None:
        self.requested = True

    def take(self) -> bool:
        requested = self.requested
        self.requested = False
        return requested


@dataclass
class CompactionResult:
    messages: list[Message]
    changed: bool
    transcript_path: str | None = None
    kept: int = 0
    freed_tokens: int = 0


def _fallback_summary(messages: list[Message]) -> str:
    head = [f"{m.role}: {m.content}" for m in messages[:10] if m.content]
    return "History truncated for context.\n" + "\n".join(head)


class ContextCompactor:
    def __init__(
        self,
        store: ContextStore,
        provider: LLMProvider,
        *,
        window: int = 1_000_000,
        trigger: float = 0.85,
        keep: int = 20,
        token_estimator: Callable[[list[Message]], int] = estimate_message_tokens,
        request: CompactRequest | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._window = window
        self._trigger = trigger
        self._keep = keep
        self._token_estimator = token_estimator
        self._request = request or CompactRequest()
        self._recent_budget = int(window * _RECENT_TOKEN_FRACTION)

    def request_compaction(self) -> None:
        self._request.set()

    async def maybe_compact(
        self, messages: list[Message], *, session_id: str | None, turn: int
    ) -> CompactionResult:
        if session_id is None:
            return CompactionResult(messages=messages, changed=False)
        threshold = int(self._window * self._trigger)
        big_enough = len(messages) > self._keep + 1 and self._token_estimator(messages) > threshold
        if not (self._request.take() or big_enough):
            return CompactionResult(messages=messages, changed=False)
        return await self._compact(messages, session_id=session_id, turn=turn)

    # -- internals -- #

    async def _compact(
        self, messages: list[Message], *, session_id: str, turn: int
    ) -> CompactionResult:
        before = self._token_estimator(messages)
        transcript_path = self._store.write_transcript(session_id, turn, messages)
        summary = await self._summarize(messages)
        recent = self._recent(messages)
        summary_msg = Message.system(
            f"{summary}\n\ncompacted transcript: {self._store.relpath(transcript_path)}"
        )
        new_messages = [messages[0], summary_msg, *recent]
        after = self._token_estimator(new_messages)
        return CompactionResult(
            messages=new_messages,
            changed=True,
            transcript_path=self._store.relpath(transcript_path),
            kept=len(recent),
            freed_tokens=before - after,
        )

    def _recent(self, messages: list[Message]) -> list[Message]:
        """Newest ``keep`` messages, bounded by the token budget (≥1 newest)."""
        recent: list[Message] = []
        tokens = 0
        for msg in reversed(messages[1:]):  # skip the system instructions
            if len(recent) >= self._keep:
                break
            t = estimate_tokens(msg.content or "")
            if recent and tokens + t > self._recent_budget:
                break
            recent.append(msg)
            tokens += t
        recent.reverse()
        return recent

    async def _summarize(self, messages: list[Message]) -> str:
        body = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt = f"{SUMMARY_PROMPT}\n\n{body}"
        try:
            resp = await self._provider.complete([Message.user(prompt)])
            text = (resp.final_text or "").strip()
            return text or _fallback_summary(messages)
        except Exception:  # noqa: BLE001 — a summary failure must never block the turn
            return _fallback_summary(messages)


def make_compact_conversation_tool(request: CompactRequest) -> Tool:
    """On-demand compaction tool; sets the request consumed next turn boundary."""

    @tool(
        name="compact_conversation",
        description=(
            "Compress the conversation history now to free context. Call this "
            "when the session feels heavy or you want to reduce context usage."
        ),
    )
    def compact_conversation(reason: str = "") -> str:
        request.set()
        return "OK — the conversation will be compacted before the next model call."

    return compact_conversation
