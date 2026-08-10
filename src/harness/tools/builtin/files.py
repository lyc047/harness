"""File-system tools: read, write, glob, grep.

These run on the local machine in P1; sandbox delegation arrives in P7.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness.tools.base import tool


@tool
def read_file(path: str) -> str:
    """Read a text file and return its contents. Path is relative to the
    current working directory unless absolute."""
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    if p.is_dir():
        return f"Error: is a directory: {path}"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error reading {path}: {exc}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file (creating parent directories if needed).
    Overwrites any existing file at that path."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"
    except OSError as exc:
        return f"Error writing {path}: {exc}"


@tool
def glob_files(pattern: str, path: str = ".") -> list[str]:
    """List files matching a glob pattern under a directory (non-recursive
    unless the pattern contains **). Returns relative paths."""
    base = Path(path)
    if not base.is_dir():
        return [f"Error: not a directory: {path}"]
    try:
        return sorted(str(p) for p in base.glob(pattern) if p.is_file())
    except Exception as exc:  # noqa: BLE001
        return [f"Error globbing {pattern}: {exc}"]


@tool
def grep_files(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
    """Search files under a directory for a regex pattern. Returns
    'file:line: matched line' entries, capped at 200 matches."""
    base = Path(path)
    if not base.is_dir():
        return f"Error: not a directory: {path}"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex {pattern!r}: {exc}"

    results: list[str] = []
    for p in sorted(base.rglob(file_pattern)):
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for lineno, line in enumerate(lines, 1):
                if regex.search(line):
                    results.append(f"{p}:{lineno}: {line.strip()}")
                    if len(results) >= 200:
                        return "\n".join(results) + "\n[truncated at 200 matches]"
        except OSError:
            continue
    return "\n".join(results) if results else "No matches found."
