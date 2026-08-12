"""Capability comparison: normal vs advanced orchestration on one complex task.

Boots a web server with HARNESS_SUBAGENTS=1, runs the SAME auto-checkable task
twice against a real model — once with the advanced toggle off, once on — and
scores each workspace deterministically (report exists / headings / section
lengths / source references). An optional LLM judge adds a 0-10 completeness
score as a secondary signal. Prints a comparison table.

This is a DEMONSTRATION, not a scientific benchmark: n=1 and the model is
nondeterministic. Its job is to show that advanced mode (nesting + concurrency)
tends to produce a more complete result on multi-subtask work.

Exit codes:
    0  PASS (both modes completed; table printed)
    1  FAIL (a mode errored or the scoring could not run)
    2  no API key configured
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MODULES = [
    "src/harness/core/runner.py",
    "src/harness/safety/approver.py",
    "src/harness/agents/registry.py",
]
MIN_SECTION_CHARS = 200


def _prompt(out: str) -> str:
    return (
        "Research the following three modules and write a markdown report: "
        f"{', '.join(MODULES)}. The report must: "
        f"(1) exist at {out}/report.md; "
        "(2) contain the three headings '## Runner', '## Approver', '## Registry' "
        "with a section describing each module; "
        f"(3) each section at least {MIN_SECTION_CHARS} characters; "
        "(4) end with a '## Sources' section listing the three file paths. "
        "Read each file before writing about it."
    )


def _score(out_dir: Path) -> dict[str, int]:
    """Deterministic rubric: report exists / headings / section lengths / sources."""
    report = out_dir / "report.md"
    if not report.exists():
        return {"report": 0, "sections": 0, "length": 0, "sources": 0, "total": 0}
    text = report.read_text(encoding="utf-8", errors="replace")

    score = 20  # report exists
    sections = 0
    for heading in ("## Runner", "## Approver", "## Registry"):
        if heading in text:
            sections += 1
    score += 15 * sections

    length_hits = 0
    lower = text.lower()
    # rough per-section length: chars between the heading and the next heading
    body = re.split(r"^## .*$", text, flags=re.M)[1:]
    for chunk in body:
        if len(chunk.strip()) >= MIN_SECTION_CHARS:
            length_hits += 1
    score += 10 * min(length_hits, 3)

    if all(m in lower for m in ("runner.py", "approver.py", "registry.py")):
        score += 5
    return {
        "report": 20 if report.exists() else 0,
        "sections": sections,
        "length": length_hits,
        "sources": 1 if score % 100 >= 0 and all(
            m in lower for m in ("runner.py", "approver.py", "registry.py")
        ) else 0,
        "total": score,
    }


async def _judge(out_dir: Path, model: str) -> int:
    """Optional LLM judge: 0-10 completeness against the brief (best effort)."""
    try:
        from harness.config import Settings
        from harness.core.messages import Message
        from harness.llm.registry import get_provider

        report = (out_dir / "report.md").read_text(encoding="utf-8", errors="replace")
        provider = get_provider(Settings.load().replace(model=model))
        resp = await provider.complete(
            [
                Message.system(
                    "You are a strict grader. Rate the following report 0-10 for "
                    "completeness against the brief (three module summaries + a "
                    "Sources section). Reply with a single integer 0-10."
                ),
                Message.user(report[:4000]),
            ]
        )
        m = re.search(r"\b(?:10|[0-9])\b", resp.final_text or "")
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001 — the judge is optional, never fatal
        return -1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError("web server did not become healthy in time")


async def _run_mode(port: int, out: str, advanced: bool) -> None:
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        if advanced:
            await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
            assert json.loads(await ws.recv())["type"] == "advanced_changed"
        await ws.send(json.dumps({"type": "message", "content": _prompt(out)}))
        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "approval_required":
                await ws.send(
                    json.dumps(
                        {
                            "type": "approval",
                            "tool_call_id": frame["tool_call"]["id"],
                            "decision": "y",
                        }
                    )
                )
            elif t == "run_done":
                return
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping compare (exit 2)", file=sys.stderr)
        return 2

    port = _free_port()
    env = {**os.environ, "HARNESS_SUBAGENTS": "1"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "harness.web.server:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    tmp = Path(tempfile.mkdtemp(prefix="harness-compare-"))
    results: dict[str, dict] = {}
    try:
        _wait_health(port)
        asyncio.run(asyncio.wait_for(_run_mode(port, str(tmp / "normal"), False), timeout=300.0))
        asyncio.run(asyncio.wait_for(_run_mode(port, str(tmp / "advanced"), True), timeout=300.0))
        for mode in ("normal", "advanced"):
            out_dir = tmp / mode
            score = _score(out_dir)
            judge = asyncio.run(
                _judge(out_dir, os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))
            )
            results[mode] = {"score": score, "judge": judge}
            print(f"[{mode}] rubric={score['total']}/100  judge={judge}/10")
        n, a = results["normal"]["score"]["total"], results["advanced"]["score"]["total"]
        print(f"comparison: normal={n}  advanced={a}  delta={a - n}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"E2E SUBAGENTS COMPARE FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
