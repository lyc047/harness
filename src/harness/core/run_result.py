"""Run results and serializable run state."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from harness.core.messages import Message


@dataclass
class RunResult:
    """Outcome of a full agent run."""

    final_output: str | None
    messages: list[Message] = field(default_factory=list)
    turns: int = 0
    session_id: str | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class MaxTurnsExceeded(RuntimeError):
    """Raised when the agent loop exceeds the configured turn budget."""

    def __init__(self, max_turns: int) -> None:
        super().__init__(f"agent exceeded max_turns={max_turns}")
        self.max_turns = max_turns


@dataclass
class RunState:
    """Serializable snapshot of an in-flight run, for pause/resume (P5).

    For now it is a plain data container; P5 wires it into the approval flow.
    """

    messages: list[Message] = field(default_factory=list)
    turns: int = 0
    max_turns: int = 30
    session_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "turns": self.turns,
                "max_turns": self.max_turns,
                "session_id": self.session_id,
                "messages": [m.to_openai_dict() for m in self.messages],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> RunState:
        data = json.loads(raw)
        return cls(
            messages=[Message.from_openai_dict(m) for m in data.get("messages", [])],
            turns=data.get("turns", 0),
            max_turns=data.get("max_turns", 30),
            session_id=data.get("session_id"),
        )
