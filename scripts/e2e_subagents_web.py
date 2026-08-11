"""End-to-end test of the web subagent run view with a real model.

Boots uvicorn with HARNESS_SUBAGENTS=1 on a free port, connects a websockets
client, sends a prompt that instructs an explicit delegation, auto-approves
each hand-off, and asserts the nested subagent frames stream to the browser:

    ready -> run_started -> approval_required -> subagent_start
            -> subagent_event (reasoning/text/tool_call/tool_result)
            -> subagent_end -> tool_result -> run_done

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
    "Call the delegate_to_researcher tool exactly once with "
    "task='Summarize in three bullets what this repo is about, read README.md first'. "
    "Then, after it returns, reply with its WHAT YOU DID line verbatim."
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

    # ping_timeout > 20s: a subagent's real-model turns can hold the loop for
    # longer than the client's default keepalive, which would otherwise 1011.
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        # 1. ready frame
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", f"expected ready, got {ready}"
        print(f"[ok] ready: session={ready['session_id']} model={ready['model']}")

        # 2. kick off a run that must delegate
        await ws.send(json.dumps({"type": "message", "content": PROMPT}))

        sub_start = 0
        sub_end = 0
        sub_ev_types: set[str] = set()
        seen: dict[str, bool] = {}
        while True:
            frame = json.loads(await ws.recv())
            seen[frame["type"]] = True
            t = frame["type"]
            if t == "approval_required":
                print(
                    f"[approval] {frame['tool_call']['name']} "
                    f"{frame['tool_call']['arguments'][:160]} -> y"
                )
                await ws.send(json.dumps({"type": "approval", "decision": "y"}))
            elif t == "subagent_start":
                sub_start += 1
                print(f"[subagent_start] agent={frame['agent']}")
            elif t == "subagent_event":
                sub_ev_types.add(frame["event"]["type"])
            elif t == "subagent_end":
                sub_end += 1
                print(
                    f"[subagent_end] agent={frame['agent']} turns={frame['turns']} "
                    f"is_error={frame['is_error']} output={frame['output'][:100]!r}"
                )
            elif t == "run_done":
                print(f"[ok] run_done: turns={frame['result']['turns']}")
                break
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")

        for required in ("run_started", "run_done"):
            assert seen.get(required), f"missing frame {required!r}; saw {sorted(seen)}"
        assert sub_start == 1, f"expected 1 subagent_start, got {sub_start}"
        assert sub_end == 1, f"expected 1 subagent_end, got {sub_end}"
        # the subagent's own turns/tools must have streamed (not just the markers)
        assert {"tool_call", "tool_result"} <= sub_ev_types, (
            f"no subagent tool frames; saw {sorted(sub_ev_types)}"
        )
        print(
            f"[ok] nested run: start={sub_start} end={sub_end} "
            f"events={sorted(sub_ev_types)}"
        )


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
        asyncio.run(asyncio.wait_for(_run(port), timeout=180.0))
        print("E2E SUBAGENTS WEB PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 — report any failure with exit 1
        print(f"E2E SUBAGENTS WEB FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
