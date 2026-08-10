"""Local sandbox: the development default.

Runs commands directly on the local machine via a subprocess. This provides
**no isolation** — it exists so development matches the tool-interface the
remote providers use. Deployments that care about isolation should use the SSH
sandbox instead.
"""

from __future__ import annotations

import asyncio

from harness.sandbox.base import SandboxResult


class LocalSandbox:
    """Runs shell commands on the local machine (NOT isolated — dev only)."""

    name = "local"

    async def run_command(
        self, command: str, *, timeout: float | None = None
    ) -> SandboxResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"timed out after {timeout}s",
                timed_out=True,
            )
        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
        )

    async def check_available(self) -> bool:
        return True


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
