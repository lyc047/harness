"""End-to-end test of the web UI with a real model (needs DEEPSEEK_API_KEY).

Boots uvicorn in the background on a free port, connects a websockets client,
sends a prompt that should trigger a tool call, auto-approves if asked, and
asserts the protocol surface end to end:

    ready -> run_started -> tool_call -> tool_result -> run_done

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
    "Call the read_file tool exactly once with path='README.md'. "
    "Then reply with the file's first line, nothing else."
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

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=2**24) as ws:
        # 1. ready frame
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", f"expected ready, got {ready}"
        print(f"[ok] ready: session={ready['session_id']} model={ready['model']}")

        # 2. kick off a run
        await ws.send(json.dumps({"type": "message", "content": PROMPT}))

        seen: dict[str, bool] = {}
        while True:
            frame = json.loads(await ws.recv())
            seen[frame["type"]] = True
            if frame["type"] == "approval_required":
                print(
                    f"[approval] {frame['tool_call']['name']} "
                    f"{frame['tool_call']['arguments']} -> y"
                )
                await ws.send(json.dumps({"type": "approval", "decision": "y"}))
            elif frame["type"] == "run_done":
                print(f"[ok] run_done: turns={frame['result']['turns']}")
                break
            elif frame["type"] == "run_error":
                raise AssertionError(f"run_error: {frame}")
            elif frame["type"] == "text":
                print(f"[text] {frame['text']!r:.80}")

        for required in ("run_started", "tool_call", "tool_result", "run_done"):
            assert seen.get(required), f"missing frame {required!r}; saw {sorted(seen)}"
        print("[ok] observed tool_call + tool_result + run_done")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping e2e (exit 2)", file=sys.stderr)
        return 2

    port = _free_port()
    env = {**os.environ}
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
        asyncio.run(asyncio.wait_for(_run(port), timeout=120.0))
        print("E2E WEB PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 — report any failure with exit 1
        print(f"E2E WEB FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
