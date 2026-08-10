"""Plan data model and statuses."""

from __future__ import annotations

from dataclasses import dataclass, field

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"

_MARKERS = {PENDING: "·", IN_PROGRESS: "→", DONE: "✓", FAILED: "✗"}


@dataclass
class PlanStep:
    """A single executable step within a :class:`Plan`."""

    id: int
    title: str
    description: str
    status: str = PENDING

    def render(self) -> str:
        return f"{_MARKERS.get(self.status, '·')} {self.id}. {self.title} — {self.description}"


@dataclass
class Plan:
    """A structured decomposition of a goal into steps."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)

    def pending_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status not in {DONE, FAILED}]

    def summary(self) -> str:
        lines = [f"目标: {self.goal}"]
        lines.extend(step.render() for step in self.steps)
        return "\n".join(lines)
