"""Task-type-aware subagent model routing.

A router maps a delegation (subagent name + brief text) to a model name, so
design/reasoning-heavy subtasks can be sent to a stronger model while
mechanical subtasks stay on the cheap default. Returning ``""`` means "use
the subagent's configured default" (no override).
"""

from __future__ import annotations

from collections.abc import Callable

# Subagents whose whole job is design/reasoning/analysis — always pro.
DESIGN_HEAVY_SUBAGENTS = frozenset(
    {"frontend_design", "security_reviewer", "researcher"}
)
# Brief-text hints that a subtask needs stronger reasoning, even for a
# normally-mechanical subagent type (e.g. coder).
REASONING_HINTS = (
    "design", "architecture", "architect", "analy", "review", "audit",
    "investigat", "research", "reason", "explain", "trade-off", "refactor",
)

# (subagent_name, task, scope) -> model name ("" = keep the configured default)
RouterFn = Callable[[str, str, str], str]


def classify_subtask(name: str, task: str, scope: str) -> str:
    """Return ``"pro"`` for design/reasoning-heavy subtasks, else ``""``."""
    if name in DESIGN_HEAVY_SUBAGENTS:
        return "pro"
    hay = f"{task} {scope}".lower()
    if any(hint in hay for hint in REASONING_HINTS):
        return "pro"
    return ""


def make_task_router(*, pro_model: str) -> RouterFn:
    """A router that maps ``"pro"`` subtasks to ``pro_model``, else default."""

    def route(name: str, task: str, scope: str) -> str:
        return pro_model if classify_subtask(name, task, scope) == "pro" else ""

    return route
