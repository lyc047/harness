"""Shell execution tool (P1: local subprocess; P7: delegated to sandbox)."""

from __future__ import annotations

import subprocess
import time

from harness.tools.base import tool


@tool
def bash(command: str, timeout: int = 60) -> str:
    """Run a shell command and return its combined stdout/stderr plus the
    exit code. On Windows the command runs via cmd.exe; use shell syntax.
    Prefer the file tools over shell for file operations."""
    if not command.strip():
        return "Error: empty command"
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s:\n$ {command}"
    except OSError as exc:
        return f"Error launching command: {exc}"

    out = proc.stdout or ""
    err = proc.stderr or ""
    parts = [f"$ {command}", f"exit code: {proc.returncode}"]
    if out.strip():
        parts.append(out.rstrip())
    if err.strip():
        parts.append(f"[stderr]\n{err.rstrip()}")
    parts.append(f"[{time.monotonic() - started:.2f}s]")
    return "\n".join(parts)
