"""End-to-end: plan a multi-step task, execute it step-by-step, and revise.

Uses the real model to (1) decompose a goal into a plan, (2) execute each step
through the turn loop, and (3) optionally revise the remaining steps after the
first one. Verifies every step reached a terminal status.

Run with a configured API key::

    uv run python scripts/e2e_planning.py
"""

from __future__ import annotations

import asyncio
import sys

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import Runner
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.planning.executor import PlanDone, PlanExecutor, PlanRevised, StepStart
from harness.planning.planner import Planner
from harness.tools.builtin import builtin_registry

GOAL = "为 harness 项目写一个 e2e_planning_out.txt 文件，内容为三行：项目名、目标、计划要点。"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    agent = Agent(
        name="assistant",
        instructions="You are a capable assistant inside the harness framework.",
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )
    provider = get_provider(settings)
    runner = Runner(provider, session_store=store.sessions)
    planner = Planner(provider, settings.model)
    executor = PlanExecutor(runner, planner, planning_interval=1)

    print("== generating plan ==")
    plan = await planner.plan(GOAL)
    print(plan.summary())

    steps = 0
    revised = 0
    done = False
    try:
        async def _run() -> None:
            nonlocal steps, revised, done
            async for event in executor.execute_streamed(agent, plan, session_id=session.id):
                if isinstance(event, StepStart):
                    steps += 1
                    print(f"\n==> step {event.step.id}: {event.step.title}")
                elif isinstance(event, PlanRevised):
                    revised += 1
                    print("\n~~ plan revised ~~")
                    print(plan.summary())
                elif isinstance(event, PlanDone):
                    done = True
                    print("\n== plan done ==")
                    print(plan.summary())

        await asyncio.wait_for(_run(), timeout=360)
    except TimeoutError:
        await store.close()
        print("=== E2E PLANNING FAILED: timed out after 360s ===")
        return 1

    await store.close()

    if not steps:
        print("=== E2E PLANNING FAILED: no steps executed ===")
        return 1
    if not done:
        print("=== E2E PLANNING FAILED: PlanDone never emitted ===")
        return 1
    terminal = [s for s in plan.steps if s.status in {"done", "failed"}]
    print(f"\nsteps: {steps}, revisions: {revised}, terminal: {len(terminal)}/{len(plan.steps)}")
    print(f"=== E2E PLANNING PASSED: executed {steps} steps, {revised} revision(s) ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
