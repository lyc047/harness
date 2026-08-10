"""Execute a plan step-by-step with periodic revision.

Each step is driven through the agent's normal turn loop (so streaming events
flow to the caller), using the active session so progress persists. Every
``planning_interval`` completed steps we ask the planner to revise the
remaining steps, then continue — smolagents' planning_interval idea.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from harness.core.agent import Agent
from harness.core.run_result import MaxTurnsExceeded
from harness.core.runner import RunDone, Runner
from harness.observability.logging import get_logger
from harness.planning.models import DONE, FAILED, IN_PROGRESS, Plan, PlanStep
from harness.planning.planner import Planner

logger = get_logger("planning")


@dataclass
class StepStart:
    plan: Plan
    step: PlanStep


@dataclass
class StepEnd:
    plan: Plan
    step: PlanStep
    output: str


@dataclass
class PlanRevised:
    plan: Plan


@dataclass
class PlanDone:
    plan: Plan


class PlanExecutor:
    def __init__(
        self, runner: Runner, planner: Planner, *, planning_interval: int = 2
    ) -> None:
        self._runner = runner
        self._planner = planner
        self._planning_interval = max(1, planning_interval)

    async def execute_streamed(
        self, agent: Agent, plan: Plan, session_id: str | None
    ) -> AsyncIterator[object]:
        """Run every step; yields stream events plus StepStart/End, PlanRevised, PlanDone."""
        progress: list[str] = []
        executed = 0
        idx = 0
        while idx < len(plan.steps):
            step = plan.steps[idx]
            step.status = IN_PROGRESS
            yield StepStart(plan=plan, step=step)

            prompt = (
                f"目标：{plan.goal}\n当前计划：\n{plan.summary()}\n"
                f"现在执行第 {idx + 1}/{len(plan.steps)} 步：{step.title} — {step.description}\n"
                "完成后简述结果。"
            )
            output = ""
            try:
                async for event in self._runner.run_streamed(
                    agent, prompt, session_id=session_id
                ):
                    yield event
                    if isinstance(event, RunDone):
                        output = event.result.final_output or ""
            except MaxTurnsExceeded:
                output = f"max_turns exceeded (step {step.id})"
                logger.warning("plan step %d hit max_turns", step.id)
            step.status = FAILED if "max_turns" in output else DONE
            progress.append(f"步骤 {step.id} [{step.title}]: {output}")
            yield StepEnd(plan=plan, step=step, output=output)

            executed += 1
            idx += 1
            if executed % self._planning_interval == 0 and plan.pending_steps():
                remaining = await self._planner.revise(plan, "\n".join(progress))
                done = [s for s in plan.steps if s.status in {DONE, FAILED}]
                plan.steps = done + remaining
                for i, s in enumerate(plan.steps, start=1):
                    s.id = i  # keep ids sequential after a revision
                idx = len(done)  # point at the first revised pending step
                yield PlanRevised(plan=plan)

        yield PlanDone(plan=plan)
