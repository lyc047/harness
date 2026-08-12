"""End-to-end test of the advanced-orchestration web run view (real model).

Boots uvicorn with HARNESS_SUBAGENTS=1, connects a websockets client, turns on
the advanced toggle, and runs two scenarios:

1. Parallel: a prompt that delegates to the researcher twice. Asserts the two
   delegated runs stream as DISTINCT-run_id subagent frames and the run
   completes:

       ready -> set_advanced -> advanced_changed -> run_started
           -> subagent_start(run_id A) ... subagent_end(A)
           -> subagent_start(run_id B) ... subagent_end(B)  (distinct run_ids)
           -> run_done

2. Nested (best effort): prompts a two-level delegation and verifies the run_id
   stack brackets depth-first and balances; the observed depth is reported, not
   hard-required (a real model may not choose to nest).

Exit codes:
    0  PASS
    1  FAIL (assertions)
    2  no API key configured
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PROMPT = (
    "Call the delegate_to_researcher tool twice, in one response if possible, "
    "with tasks: (1) 'Summarize what README.md says in three bullets' and "
    "(2) 'Summarize what docs/architecture.md says in three bullets'. "
    "Then, after both return, reply with both WHAT YOU DID lines."
)


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
        except Exception:  # noqa: BLE001 — server may still be booting
            time.sleep(0.5)
    raise RuntimeError("web server did not become healthy in time")


async def _run(port: int) -> None:
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        assert ready["subagents"] is True

        await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
        advanced = json.loads(await ws.recv())
        assert advanced["type"] == "advanced_changed" and advanced["advanced"] is True

        await ws.send(json.dumps({"type": "message", "content": PROMPT}))
        run_ids: set[str] = set()
        starts = ends = 0
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
            elif t == "subagent_start":
                starts += 1
                run_ids.add(frame["run_id"])
                print(f"[subagent_start] run={frame['run_id'][:8]} agent={frame['agent']}")
            elif t == "subagent_end":
                ends += 1
                print(f"[subagent_end] run={frame['run_id'][:8]} turns={frame['turns']}")
            elif t == "run_done":
                print(f"[ok] run_done: turns={frame['result']['turns']}")
                break
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")

        assert starts >= 2, f"expected >=2 subagent starts, got {starts}"
        assert ends == starts, f"subagent_end {ends} != subagent_start {starts}"
        assert len(run_ids) == starts, (
            f"run_ids not unique per run: {sorted(run_ids)}"
        )
        print(f"[ok] advanced run: starts={starts} ends={ends} distinct_run_ids={len(run_ids)}")


NESTED_PROMPT = (
    "Delegate to the researcher exactly once with this task: "
    "'Research what src/harness/core/runner.py does, then delegate to doc_writer "
    "with task=\"Write a short summary of it to nested_report.md\". After the "
    "researcher returns, reply with its WHAT YOU DID line."
)


async def _run_nested(port: int) -> None:
    """Best-effort two-level nesting check (spec 10.2 nested scenario).

    A real model may or may not choose to nest — the prompt can only request it.
    So we verify what is guaranteed: the run completes, subagent start/end
    frames bracket depth-first (each end matches the top of the client's run_id
    stack), and the observed depth is reported. The deterministic depth-2 proof
    lives in the unit tests (test_advanced_nested_delegation_two_levels).
    """
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
        assert json.loads(await ws.recv())["type"] == "advanced_changed"

        await ws.send(json.dumps({"type": "message", "content": NESTED_PROMPT}))
        stack: list[str] = []
        max_depth = 0
        starts = 0
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
            elif t == "subagent_start":
                stack.append(frame["run_id"])
                max_depth = max(max_depth, len(stack))
                starts += 1
                print(
                    f"[nested] start depth={len(stack)} run={frame['run_id'][:8]} "
                    f"agent={frame['agent']}"
                )
            elif t == "subagent_end":
                # depth-first: the ending run is always the stack top
                assert stack and stack[-1] == frame["run_id"], "subagent_end out of order"
                stack.pop()
                print(f"[nested] end depth={len(stack)} run={frame['run_id'][:8]}")
            elif t == "run_done":
                break
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")
        assert starts >= 1, "no subagent run started"
        assert not stack, f"unbalanced subagent stack: {stack}"
        print(f"[ok] nested: starts={starts} max_depth={max_depth} stack_balanced=True")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping e2e (exit 2)", file=sys.stderr)
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
    try:
        _wait_health(port)
        asyncio.run(asyncio.wait_for(_run(port), timeout=300.0))
        asyncio.run(asyncio.wait_for(_run_nested(port), timeout=300.0))
        print("E2E SUBAGENTS ADVANCED WEB PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 — report any failure with exit 1
        print(f"E2E SUBAGENTS ADVANCED WEB FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
