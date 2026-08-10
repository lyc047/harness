"""Generate and revise step plans from the LLM.

The planner asks the model to emit strict JSON (``tools=None`` so it is forced
to answer in text), parses it tolerantly, and validates the shape. ``revise``
re-plans only the remaining steps mid-execution.
"""

from __future__ import annotations

import json
import re
from typing import Any

from harness.core.messages import Message
from harness.llm.base import LLMProvider
from harness.observability.logging import get_logger
from harness.planning.models import Plan, PlanStep

logger = get_logger("planning")

PLAN_SYSTEM_PROMPT = """\
You are a planning assistant. Given a user goal, produce a concise, actionable
plan. Think about the minimal set of steps needed to complete the goal; each
step should be specific enough to execute and verify on its own.

Respond with JSON ONLY, no prose, in exactly this shape:
{"goal": "<restated goal>", "steps": [{"title": "<short title>", "description": "<what to do>"}]}
Use 2-8 steps.
"""

REVISE_SYSTEM_PROMPT = """\
You are revising an execution plan. You are given the goal, the current plan
with step statuses, and the results of completed steps. Decide the remaining
steps: keep them, split them, merge them, add new ones, or drop ones no longer
needed.

Respond with JSON ONLY, no prose: a JSON array of the REMAINING (not yet done)
steps, e.g. [{"title": "...", "description": "..."}]. If nothing remains, [].
"""


def extract_json(text: str) -> Any:
    """Best-effort parse of a JSON object/array embedded in LLM text.

    Handles plain JSON, ```json fences, and prose wrapped around a JSON block.
    Returns ``None`` when nothing parseable is found.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    candidates: list[str] = [text]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        if open_ch in text and close_ch in text:
            candidates.append(text[text.index(open_ch) : text.rindex(close_ch) + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


class Planner:
    """Turns a goal into a :class:`Plan` and revises it mid-execution."""

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        *,
        max_steps: int = 8,
        retries: int = 2,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_steps = max_steps
        self._retries = retries

    async def plan(self, goal: str) -> Plan:
        data = await self._ask(PLAN_SYSTEM_PROMPT, f"用户目标：{goal}\n请给出执行计划（JSON）。")
        if not isinstance(data, dict):
            raise ValueError("planner returned a non-object plan")
        raw_steps = data.get("steps", [])
        steps = [
            PlanStep(
                id=i + 1,
                title=str(s.get("title", f"step {i + 1}")),
                description=str(s.get("description", "")),
            )
            for i, s in enumerate(raw_steps[: self._max_steps])
        ]
        if not steps:
            raise ValueError("planner returned no steps")
        return Plan(goal=str(data.get("goal", goal)), steps=steps)

    async def revise(self, plan: Plan, progress: str) -> list[PlanStep]:
        """Re-plan the remaining steps given what has been completed."""
        prompt = (
            f"目标：{plan.goal}\n当前计划：\n{plan.summary()}\n"
            f"已完成步骤的结果：\n{progress}\n请返回剩余步骤的 JSON 数组。"
        )
        data = await self._ask(REVISE_SYSTEM_PROMPT, prompt)
        if not isinstance(data, list):
            return []
        offset = len(plan.steps)
        return [
            PlanStep(
                id=offset + i + 1,
                title=str(s.get("title", f"step {offset + i + 1}")),
                description=str(s.get("description", "")),
            )
            for i, s in enumerate(data)
        ]

    async def _ask(self, system_prompt: str, prompt: str) -> Any:
        last_exc: Exception | None = None
        for _ in range(self._retries):
            resp = await self._provider.complete(
                [Message.system(system_prompt), Message.user(prompt)], tools=None
            )
            data = extract_json(resp.final_text or "")
            if data is not None:
                return data
            last_exc = ValueError("planner returned non-JSON output")
        raise last_exc or RuntimeError("planner failed")
