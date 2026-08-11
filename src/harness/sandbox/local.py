"""Local sandbox: the development default.

Runs commands directly on the local machine via a subprocess. This provides
**no isolation** — it exists so development matches the tool-interface the
remote providers use. Deployments that care about isolation should use the SSH
sandbox instead.
"""

from __future__ import annotations

import asyncio
import shutil

from harness.sandbox.base import SandboxResult


def find_bash() -> str | None:
    """Locate a POSIX shell (Git Bash / MSYS / /usr/bin/bash), if any.

    The models this harness drives are trained on Unix shell syntax
    (``$(pwd)``, ``2>&1``, ``| tail``, ``&&``) which Windows ``cmd.exe`` cannot
    run. When a bash is on PATH — always true under Git Bash, the dev default on
    Windows — commands run through it; otherwise we fall back to the platform
    shell (``cmd.exe`` on Windows).
    """
    return shutil.which("bash")


class LocalSandbox:
    """Runs shell commands on the local machine (NOT isolated — dev only)."""

    name = "local"

    def __init__(self) -> None:
        self._bash = find_bash()

    async def run_command(
        self, command: str, *, timeout: float | None = None
    ) -> SandboxResult:
        if self._bash:
            proc = await asyncio.create_subprocess_exec(
                self._bash,
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
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
    """Decode subprocess output.

    Git Bash / MSYS emit UTF-8, but a bare Windows shell writes the ANSI/OEM
    codepage (GBK on zh-CN). Try UTF-8 first, then the ANSI codepage, so error
    messages stay human-readable instead of mojibake.
    """
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
