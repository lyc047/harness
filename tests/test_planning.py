"""Tests for planning: plan generation, revision, and step execution."""

from __future__ import annotations

import pytest

from harness.core.agent import Agent
from harness.core.runner import Runner
from harness.llm.base import LLMResponse
from harness.planning.executor import PlanDone, PlanExecutor, PlanRevised, StepEnd, StepStart
from harness.planning.models import DONE, PENDING
from harness.planning.planner import Planner, extract_json

# -- JSON extraction --

def test_extract_json_plain() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_inside_prose() -> None:
    text = "Here is the plan: {\"goal\": \"g\", \"steps\": []}. That's all."
    assert extract_json(text) == {"goal": "g", "steps": []}


def test_extract_json_array() -> None:
    assert extract_json('[{"title": "a"}]') == [{"title": "a"}]


def test_extract_json_garbage() -> None:
    assert extract_json("not json at all") is None


# -- Planner --

async def test_planner_generates_plan(make_provider) -> None:
    provider = make_provider(
        script=[
            LLMResponse(
                final_text='{"goal": "g", "steps": [{"title": "a", "description": "A"}, '
                '{"title": "b", "description": "B"}]}'
            )
        ]
    )
    plan = await Planner(provider, "m").plan("do g")
    assert plan.goal == "g"
    assert [s.title for s in plan.steps] == ["a", "b"]
    assert all(s.status == PENDING for s in plan.steps)


async def test_planner_retries_on_bad_json(make_provider) -> None:
    provider = make_provider(
        script=[
            LLMResponse(final_text="sorry, here is my reasoning... no json"),
            LLMResponse(final_text='{"goal": "g", "steps": [{"title": "a", "description": "A"}]}'),
        ]
    )
    plan = await Planner(provider, "m").plan("x")
    assert plan.steps[0].title == "a"


async def test_planner_raises_when_always_bad(make_provider) -> None:
    provider = make_provider(
        script=[LLMResponse(final_text="nope"), LLMResponse(final_text="still nope")]
    )
    with pytest.raises(ValueError):
        await Planner(provider, "m", retries=2).plan("x")


# -- Executor --

async def test_executor_runs_steps_and_revises(make_provider) -> None:
    script = [
        # plan generation
        LLMResponse(
            final_text='{"goal": "g", "steps": [{"title": "s1", "description": "d1"}, '
            '{"title": "s2", "description": "d2"}]}'
        ),
        # step 1 execution
        LLMResponse(final_text="step 1 output"),
        # revision (planning_interval=1)
        LLMResponse(final_text='[{"title": "s2 revised", "description": "d2"}]'),
        # step 2 execution
        LLMResponse(final_text="step 2 output"),
    ]
    provider = make_provider(script)
    agent = Agent(name="a", instructions="i", model="m")
    runner = Runner(provider)
    planner = Planner(provider, "m")

    plan = await planner.plan("g")
    executor = PlanExecutor(runner, planner, planning_interval=1)
    events = [e async for e in executor.execute_streamed(agent, plan, session_id=None)]

    starts = [e for e in events if isinstance(e, StepStart)]
    ends = [e for e in events if isinstance(e, StepEnd)]
    assert len(starts) == 2
    assert len(ends) == 2
    assert ends[0].output == "step 1 output"
    assert ends[1].output == "step 2 output"

    # the revision replaced the remaining step
    assert any(isinstance(e, PlanRevised) for e in events)
    assert any(isinstance(e, PlanDone) for e in events)
    assert plan.steps[-1].title == "s2 revised"
    # completed steps are marked done
    assert {s.title: s.status for s in plan.steps} == {"s1": DONE, "s2 revised": DONE}


async def test_executor_without_revision(make_provider) -> None:
    script = [
        LLMResponse(
            final_text='{"goal": "g", "steps": [{"title": "a", "description": "A"}, '
            '{"title": "b", "description": "B"}]}'
        ),
        LLMResponse(final_text="a out"),
        LLMResponse(final_text="b out"),
    ]
    provider = make_provider(script)
    agent = Agent(name="a", instructions="i", model="m")
    runner = Runner(provider)
    planner = Planner(provider, "m")

    plan = await planner.plan("g")
    # planning_interval larger than the step count => no revision call
    executor = PlanExecutor(runner, planner, planning_interval=10)
    events = [e async for e in executor.execute_streamed(agent, plan, session_id=None)]
    assert not any(isinstance(e, PlanRevised) for e in events)
    assert len([e for e in events if isinstance(e, StepEnd)]) == 2
    assert provider.stream_calls == [2, 2, 2]  # plan, step1, step2 (no revision)
