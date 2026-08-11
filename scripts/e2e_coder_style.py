"""End-to-end: the coder subagent produces lint-clean, tested code.

Drives the built-in ``coder`` subagent through the full real stack (the same
``build_core_stack`` the web/CLI use, so bash runs via the sandbox), asking it
to write a quicksort + pytest test and verify with ``ruff`` + ``pytest``. After
the run we independently re-check that the files pass the project lint and
tests, then remove them (they are scratch, not repo code).

Run with a configured API key::

    uv run python scripts/e2e_coder_style.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from harness.agents.examples import coder
from harness.config import Settings
from harness.core.runner import RunDone, ToolResultEvent
from harness.llm.base import StreamText
from harness.memory.store import Store

FILES = ["quicksort.py", "test_quicksort.py"]

TASK = (
    "在仓库根目录创建 quicksort.py,实现一个快速排序(quicksort)函数;"
    "再创建 test_quicksort.py,用 pytest 测试它。"
    "然后依次运行 `uv run pytest -q test_quicksort.py` 和 "
    "`uv run ruff check quicksort.py test_quicksort.py`,"
    "确保测试全部通过且 ruff 无任何报错;若 ruff 有报错就修改代码直到它通过。"
    "最后简要报告两个命令的实际输出。"
)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


async def _auto_approve(_tool_call: object) -> str:
    return "y"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def _verify_and_cleanup() -> int:
    """Sync: independently check the generated files, then delete them."""
    result = 0
    try:
        files = [f for f in FILES if Path(f).exists()]
        if not files:
            print("=== E2E CODER FAILED: no generated files found ===")
            return 1
        ruff = _run(["uv", "run", "ruff", "check", *files])
        print(f"ruff check exit={ruff.returncode}\n{ruff.stdout[-400:]}")
        if ruff.returncode != 0:
            result = 1
        if "test_quicksort.py" in files:
            pytest = _run(["uv", "run", "pytest", "-q", "test_quicksort.py"])
            print(f"pytest exit={pytest.returncode}\n{pytest.stdout[-300:]}")
            if pytest.returncode != 0:
                result = 1
    finally:
        for f in FILES:
            Path(f).unlink(missing_ok=True)
    return result


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    from harness.agents.orchestrator import add_subagents
    from harness.core.compose import build_core_stack

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()
    stack = await build_core_stack(settings, store=store, prompt=_auto_approve)
    add_subagents(stack.agent, stack.runner, [coder()])

    final = ""
    errors: list[str] = []
    try:
        async for event in stack.runner.run_streamed(
            stack.agent, TASK, session_id=session.id
        ):
            if isinstance(event, StreamText):
                sys.stdout.write(event.text)
                sys.stdout.flush()
            elif isinstance(event, ToolResultEvent):
                if event.result.is_error:
                    errors.append(event.result.content[:200])
            elif isinstance(event, RunDone):
                final = event.result.final_output or ""
    finally:
        await store.close()

    print(f"\nfinal output: {final[:400]}\n")

    # Independent verification of the files the coder left behind.
    result = _verify_and_cleanup()

    if result == 0:
        print("=== E2E CODER PASSED: lint-clean, tests green ===")
    else:
        print(f"=== E2E CODER FAILED (tool errors: {len(errors)}) ===")
    return result


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
