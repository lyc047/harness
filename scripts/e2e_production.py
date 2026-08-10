"""End-to-end production scenario exercising the full harness stack.

One real-model run exercises, in a single composite scenario:

- **planning**   — Planner decomposes the goal, PlanExecutor runs it step by step
- **approval**   — every tool call goes through ApprovalExecutor (ASK policy,
  scripted "y" prompt standing in for a human)
- **sandbox**    — `bash` calls route through LocalSandbox (recording wrapper
  verifies they did)
- **skills**     — the model authors a skill at runtime via `create_skill`
- **persistence**— messages + checkpoints in SQLite
- **tracing**    — a Tracer records run/turn/tool events to JSONL

Run with a configured API key::

    uv run python scripts/e2e_production.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import Runner, default_executor
from harness.llm.registry import get_provider
from harness.memory.preferences import make_remember_preference_tool
from harness.memory.store import Store
from harness.observability.tracing import Tracer
from harness.planning.executor import PlanDone, PlanExecutor, StepStart
from harness.planning.planner import Planner
from harness.safety.approver import ApprovalExecutor
from harness.safety.permissions import Permission, Permissions
from harness.sandbox import LocalSandbox, SandboxedExecutor, SandboxResult
from harness.skills.loader import make_create_skill_tool
from harness.skills.registry import SkillRegistry
from harness.tools.builtin import builtin_registry

SKILL_NAME = "prod-note"
REPORT_NAME = "e2e_production_report.txt"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


class RecordingSandbox:
    """Wrap the local sandbox and record every command routed to it."""

    name = "recording"

    def __init__(self, inner: LocalSandbox) -> None:
        self._inner = inner
        self.commands: list[str] = []

    async def run_command(self, command: str, *, timeout: float | None = None) -> SandboxResult:
        self.commands.append(command)
        return await self._inner.run_command(command, timeout=timeout)

    async def check_available(self) -> bool:
        return await self._inner.check_available()


async def _scripted_yes(_tool_call: object) -> str:
    """Programmatic approver: approve every ASK-decided tool call."""
    return "y"


def _file_exists(path: str) -> bool:
    return Path(path).exists()


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="harness-prod-e2e-"))
    skills_dir = tmp / "skills"
    workdir = tmp / "workspace"
    os.makedirs(skills_dir)
    os.makedirs(workdir)
    report_path = workdir / REPORT_NAME
    skill_file = skills_dir / f"{SKILL_NAME}.md"

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    registry = SkillRegistry(skills_dir)
    registry.discover()

    agent = Agent(
        name="assistant",
        instructions=(
            "You are a capable assistant inside the harness framework. You have "
            "tools for files, shell, skills and preferences. Use them when asked."
        ),
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )
    agent.tools.register(make_create_skill_tool(registry))
    agent.tools.register(make_remember_preference_tool(store.preferences))

    # Full executor chain: approval -> sandbox -> default.
    recording = RecordingSandbox(LocalSandbox())
    sandboxed = SandboxedExecutor(default_executor, recording)
    approval = ApprovalExecutor(
        sandboxed,
        Permissions(default=Permission.ASK),
        prompt=_scripted_yes,
    )

    trace_buf = io.StringIO()
    tracer = Tracer(trace_buf)
    provider = get_provider(settings)
    runner = Runner(
        provider,
        session_store=store.sessions,
        tool_executor=approval,
        hooks=tracer.make_hooks(),
    )
    planner = Planner(provider, settings.model)
    executor = PlanExecutor(runner, planner, planning_interval=1)

    report = report_path.as_posix()
    goal = (
        f"请完成三项子任务：\n"
        f"1. 调用 create_skill 创建一个名为 {SKILL_NAME} 的 skill："
        f"description 为 '写生产就绪备注'，content 为 '1. 文件须有标题。2. 检查文件是否存在。'\n"
        f"2. 调用 write_file 把下面三行内容写入 {report}：\n"
        f"harness\nP8 end-to-end\nproduction-ready\n"
        f"3. 调用 bash 运行 python -c \"import os; print(os.path.exists("
        f"'{report}'))\" 验证文件存在，并报告输出"
    )

    print("== generating plan ==")
    plan = await planner.plan(goal)
    print(plan.summary())

    steps = 0
    done = False
    try:
        async def _run() -> None:
            nonlocal steps, done
            async for event in executor.execute_streamed(agent, plan, session_id=session.id):
                if isinstance(event, StepStart):
                    steps += 1
                    print(f"\n==> step {event.step.id}: {event.step.title}")
                elif isinstance(event, PlanDone):
                    done = True
                    print("\n== plan done ==")

        await asyncio.wait_for(_run(), timeout=600)
    except TimeoutError:
        await store.close()
        print("=== E2E PRODUCTION FAILED: timed out after 600s ===")
        return 1
    await store.close()

    trace_lines = trace_buf.getvalue().strip().splitlines()
    trace_types = [json.loads(line)["type"] for line in trace_lines if line.strip()]

    problems: list[str] = []
    if not done:
        problems.append("PlanDone never emitted")
    if not steps:
        problems.append("no steps executed")
    if not skill_file.exists() or registry.get(SKILL_NAME) is None:
        problems.append(f"skill '{SKILL_NAME}' not created/indexed")
    if not _file_exists(str(report_path)):
        problems.append(f"report file missing: {report_path}")
    if not recording.commands:
        problems.append("no bash command routed through the sandbox")
    if "run_end" not in trace_types or "tool_result" not in trace_types:
        problems.append("trace missing expected events")

    if problems:
        print("=== E2E PRODUCTION FAILED ===")
        for p in problems:
            print(f"  - {p}")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    print(f"[ok] steps={steps}, bash_through_sandbox={len(recording.commands)}")
    print(f"[ok] skill={skill_file.name}, report={report_path.name}")
    print(f"[ok] trace: {len(trace_lines)} lines, {len(set(trace_types))} event types")
    print("=== E2E PRODUCTION PASSED ===")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
