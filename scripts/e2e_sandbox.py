"""End-to-end: bash tool calls route through the configured sandbox.

The real model is asked to run shell commands. A recording wrapper around the
local sandbox verifies every ``bash`` call the model makes flows through the
sandbox layer and that command output reaches the final answer.

Run with a configured API key::

    uv run python scripts/e2e_sandbox.py
"""

from __future__ import annotations

import asyncio
import sys

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import RunDone, Runner, default_executor
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.sandbox import LocalSandbox, SandboxedExecutor, SandboxResult
from harness.tools.builtin import builtin_registry


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


class RecordingSandbox:
    """Wrap a real sandbox and record every command routed to it."""

    name = "recording"

    def __init__(self, inner: LocalSandbox) -> None:
        self._inner = inner
        self.commands: list[str] = []

    async def run_command(self, command: str, *, timeout: float | None = None) -> SandboxResult:
        self.commands.append(command)
        return await self._inner.run_command(command, timeout=timeout)

    async def check_available(self) -> bool:
        return await self._inner.check_available()


async def main() -> int:
    _force_utf8_stdio()
    settings = Settings.load()
    if not settings.api_key:
        print("No DEEPSEEK_API_KEY configured.")
        return 2

    store = Store(settings)
    await store.initialize()
    session = await store.sessions.create_session()

    recording = RecordingSandbox(LocalSandbox())
    executor = SandboxedExecutor(default_executor, recording)

    agent = Agent(
        name="sandbox-probe",
        instructions=(
            "You have a bash tool. Run shell commands when asked and report "
            "their output to the user verbatim."
        ),
        tools=builtin_registry(),
        model=settings.model,
        max_turns=8,
    )
    runner = Runner(
        get_provider(settings), session_store=store.sessions, tool_executor=executor
    )

    prompt = (
        "请用 bash 工具依次运行下面两条命令，并把输出报告给我：\n"
        "1. echo sandbox-e2e-hello\n"
        "2. python --version"
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
        print("=== E2E SANDBOX FAILED: timed out ===")
        return 1
    await store.close()

    if not recording.commands:
        print("=== E2E SANDBOX FAILED: no bash calls routed through sandbox ===")
        return 1
    if "sandbox-e2e-hello" not in final:
        print(
            "=== E2E SANDBOX FAILED: final output missing sandbox marker "
            f"(commands={recording.commands!r})"
        )
        return 1
    print(
        f"[ok] bash routed through sandbox: {len(recording.commands)} command(s) "
        f"-> {final[:80]!r}"
    )
    print("=== E2E SANDBOX PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
