"""End-to-end: self-evolving skills and user preferences against the real model.

Scenarios:

1. The model authors a new skill at runtime via the ``create_skill`` tool.
   We assert the file exists and the registry indexes it.
2. Persistence: a fresh session whose agent injects the skill directory still
   sees the skill ("restart" survives).
3. The model records a stated user preference via ``remember_preference`` and
   it persists in SQLite.

Run with a configured API key::

    uv run python scripts/e2e_skills.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import RunDone, Runner
from harness.llm.registry import get_provider
from harness.memory.preferences import make_remember_preference_tool
from harness.memory.store import Store
from harness.skills.loader import make_create_skill_tool
from harness.skills.registry import SkillRegistry
from harness.tools.builtin import builtin_registry

SKILL_NAME = "e2e-format"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _skill_file_exists(skills_dir: Path) -> bool:
    return (skills_dir / f"{SKILL_NAME}.md").exists()


def _cleanup(skills_dir: Path) -> None:
    shutil.rmtree(skills_dir, ignore_errors=True)


async def _scenario_self_create_skill(settings: Settings, skills_dir: Path) -> int:
    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    registry = SkillRegistry(skills_dir)
    registry.discover()
    agent = Agent(
        name="skill-author",
        instructions=(
            "You have a create_skill tool. When asked, author the skill "
            "exactly as requested: name, description, and content."
        ),
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )
    agent.tools.register(make_create_skill_tool(registry))
    runner = Runner(get_provider(settings), session_store=store.sessions)

    prompt = (
        f"请调用 create_skill 创建名为 {SKILL_NAME} 的 skill："
        f"description 为 'Format Python with ruff'，"
        f"content 为 '1. Run ruff check .\\n2. Run ruff format .\\n3. Run mypy src'"
    )
    final = ""
    try:
        async def _run() -> None:
            nonlocal final
            async for event in runner.run_streamed(agent, prompt, session_id=session.id):
                if isinstance(event, RunDone):
                    final = event.result.final_output or ""

        await asyncio.wait_for(_run(), timeout=360)
    except TimeoutError:
        await store.close()
        print("=== E2E SKILLS FAILED: scenario 1 timed out ===")
        return 1
    await store.close()

    if not _skill_file_exists(skills_dir) or registry.get(SKILL_NAME) is None:
        indexed = registry.get(SKILL_NAME) is not None
        print(
            "=== E2E SKILLS FAILED: skill not created "
            f"(file={_skill_file_exists(skills_dir)}, indexed={indexed})"
        )
        return 1
    print(f"[ok] scenario 1: model self-created skill '{SKILL_NAME}' -> {final[:60]!r}")
    return 0


def _scenario_persistence(skills_dir: Path) -> int:
    """A fresh agent injecting the skills dir must still see the skill."""
    registry = SkillRegistry(skills_dir)
    registry.discover()
    if registry.get(SKILL_NAME) is None:
        print("=== E2E SKILLS FAILED: skill missing after 'restart' ===")
        return 1
    instructions = registry.inject("You are an assistant.")
    if SKILL_NAME not in instructions or "ruff" not in instructions:
        print("=== E2E SKILLS FAILED: skill not injected into instructions ===")
        return 1
    print("[ok] scenario 2: skill survives a fresh session and injects")
    return 0


async def _scenario_preference(settings: Settings) -> int:
    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    agent = Agent(
        name="pref-recorder",
        instructions=(
            "You have a remember_preference tool. When the user states a "
            "durable preference, persist it with that tool."
        ),
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )
    agent.tools.register(make_remember_preference_tool(store.preferences))
    runner = Runner(get_provider(settings), session_store=store.sessions)

    try:
        async def _run() -> None:
            async for _ in runner.run_streamed(
                agent, "记住我的偏好：答复语言 = 中文", session_id=session.id
            ):
                pass

        await asyncio.wait_for(_run(), timeout=360)
    except TimeoutError:
        await store.close()
        print("=== E2E SKILLS FAILED: scenario 3 timed out ===")
        return 1

    stored = await store.preferences.get("答复语言")
    await store.close()
    if not stored:
        print("=== E2E SKILLS FAILED: preference not stored ===")
        return 1
    print(f"[ok] scenario 3: preference persisted -> 答复语言 = {stored!r}")
    return 0


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="harness-skills-e2e-"))
    try:
        if await _scenario_self_create_skill(settings, tmp) != 0:
            return 1
        if _scenario_persistence(tmp) != 0:
            return 1
        if await _scenario_preference(settings) != 0:
            return 1
    finally:
        _cleanup(tmp)

    print("=== E2E SKILLS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
