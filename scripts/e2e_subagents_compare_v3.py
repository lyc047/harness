"""Depth-2 forcing experiment: can a real model chain a level-2 subagent?

Same harness as e2e_subagents_compare_v2.py but the task is reshaped to make
deeper delegation the ONLY natural path to completion:

  * the PARENT delegates the ENTIRE audit to a single ``coordinator`` subagent
    and does no research/writing itself;
  * the ``coordinator`` is allowlisted to read/glob/grep/web_search ONLY — no
    write_file, no bash (a deliberately-created runtime subagent);
  * the deliverable must exist on disk as a report file -> the coordinator
    cannot write it, so it MUST hand off to the ``doc_writer`` subagent ->
    parent -> coordinator -> doc_writer is depth 2.

Only the advanced mode is run (depth-2 nesting is structurally unavailable in
normal mode), so the v2 advanced baseline (depth=1, total=85.7) is the control.

Scores reuse v2's deterministic rubric (A structure / B resource / C outcome).
Exit codes: 0 ok, 1 fail, 2 no API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from e2e_subagents_compare_v2 import (
    HEADINGS,
    REPO_ROOT,
    _fmt_spread,
    _free_port,
    _score_run,
    _structure_score,
    _wait_health,
)

RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))
DELEGATE_PREFIX = "delegate_to_"

COORDINATOR_NAME = "coordinator"
COORDINATOR_YAML = (
    REPO_ROOT / "skills" / "subagents" / f"{COORDINATOR_NAME}.yaml"
)

# The coordinator ships bundled (src/harness/skills/bundled/subagents/
# coordinator.yaml); the prompt above tells it how to run this audit. No
# runtime write — a stale skills/subagents/coordinator.yaml from a killed run
# shadows the bundled set and breaks subagent-description tests.


def _prompt(out: str) -> str:
    headings = ", ".join(f"'{h}'" for h in HEADINGS)
    return (
        "Perform the following audit and produce a markdown report.\n"
        "DELEGATE THE ENTIRE TASK to a single subagent — call the "
        f"delegate_to_{COORDINATOR_NAME} tool once and hand it the FULL task "
        "below plus the target report path. Do NOT research, read, search, or "
        "write anything yourself — the coordinator owns the whole job.\n\n"
        "TASK: audit src/harness/ (every .py under it) for all places that "
        "communicate with anything OUTSIDE the local Python interpreter — "
        "outbound HTTP (urllib/urlopen, OpenAI-compatible HTTP clients), SSH, "
        "WebSocket server endpoints, MCP stdio transports, and local subprocess "
        "launches. For each, record the exact `path:line`, the transport, and "
        "the peer/service it talks to.\n"
        "For the external services those touchpoints talk to, cite each one's "
        "CURRENT official documentation URL (a real https URL — use the "
        "web_search tool; never invent one). Use AT MOST ONE web_search call "
        "per service.\n"
        f"Write the report to {out}/report.md with sections {headings}.\n"
        "NOTE: the coordinator has no write access. It is expected to hand the "
        "writing to the doc_writer subagent, which CAN write the file. Wait for "
        "the coordinator's summary and report back."
    )


async def _run_mode(port: int, out: str, advanced: bool) -> dict[str, object]:
    """Run one advanced-mode run; track delegation depth + tool usage + chain."""
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

        started = time.monotonic()
        active = 0
        max_concurrency = 0
        depth_by_run: dict[str, int] = {}
        agent_by_run: dict[str, str] = {}
        parent_by_run: dict[str, str | None] = {}
        pending_delegator: str | None = None
        types: set[str] = set()
        sub_turns = 0
        waves = 0
        last_was_delegate_call = False
        tool_uses: dict[str, int] = {}

        def _count(name: str) -> None:
            tool_uses[name] = tool_uses.get(name, 0) + 1

        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "tool_call":  # parent's own tool call
                name = frame["tool_call"]["name"]
                if name.startswith(DELEGATE_PREFIX):
                    if not last_was_delegate_call:
                        waves += 1
                    last_was_delegate_call = True
                    pending_delegator = "root"
                else:
                    last_was_delegate_call = False
                    _count(name)
            elif t == "subagent_event":
                ev = frame["event"]
                if ev.get("type") == "tool_call":
                    ev_name = ev["tool_call"]["name"]
                    if ev_name.startswith(DELEGATE_PREFIX):
                        pending_delegator = frame["run_id"]
                    else:
                        _count(ev_name)
                last_was_delegate_call = False
            elif t == "subagent_start":
                types.add(frame["agent"])
                agent_by_run[frame["run_id"]] = frame["agent"]
                parent = (
                    None
                    if pending_delegator is None or pending_delegator == "root"
                    else pending_delegator
                )
                parent_by_run[frame["run_id"]] = parent
                base = (
                    0
                    if pending_delegator is None or pending_delegator == "root"
                    else depth_by_run.get(pending_delegator, 0)
                )
                depth_by_run[frame["run_id"]] = base + 1
                pending_delegator = None
                active += 1
                max_concurrency = max(max_concurrency, active)
                last_was_delegate_call = False
            elif t == "subagent_end":
                active -= 1
                sub_turns += int(frame.get("turns", 0))
                last_was_delegate_call = False
            elif t == "approval_required":
                await ws.send(
                    json.dumps(
                        {
                            "type": "approval",
                            "tool_call_id": frame["tool_call"]["id"],
                            "decision": "y",
                        }
                    )
                )
                last_was_delegate_call = False
            elif t == "run_done":
                def _path(run_id: str) -> list[str]:
                    path: list[str] = []
                    cur: str | None = run_id
                    while cur is not None:
                        path.append(agent_by_run[cur])
                        cur = parent_by_run.get(cur)
                    return path[::-1]

                chains = sorted(
                    {tuple(_path(rid)) for rid in depth_by_run},
                    key=lambda p: (len(p), p),
                )
                return {
                    "seconds": time.monotonic() - started,
                    "delegations": len(depth_by_run),
                    "waves": waves,
                    "max_concurrency": max_concurrency,
                    "depth": max(depth_by_run.values(), default=0),
                    "types": len(types),
                    "sub_turns": sub_turns,
                    "web_searches": tool_uses.get("web_search", 0),
                    "greps": tool_uses.get("grep_files", 0) + tool_uses.get("glob_files", 0),
                    "writes": tool_uses.get("write_file", 0),
                    "bash": tool_uses.get("bash", 0),
                    "chain": [list(c) for c in chains],
                }
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")
            else:
                last_was_delegate_call = False


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
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
    server_log: deque[str] = deque(maxlen=60)

    def _drain_server() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            server_log.append(raw.decode("utf-8", errors="replace").rstrip())

    threading.Thread(target=_drain_server, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="harness-depth2-"))
    runs: list[dict[str, Any]] = []
    try:
        _wait_health(port)
        for i in range(1, RUNS + 1):
            out_dir = tmp / f"advanced-{i}"
            try:
                metrics = asyncio.run(
                    asyncio.wait_for(_run_mode(port, str(out_dir), True), timeout=600.0)
                )
            except TimeoutError:
                print(f"  advanced-{i}: TIMEOUT after 600s — skipped", flush=True)
                continue
            runs.append({"mode": "advanced", "out": out_dir, "metrics": metrics, "run": i})
            struct = _structure_score(out_dir)
            chains = [" -> ".join(c) for c in cast(list[list[str]], metrics["chain"])]
            print(
                f"  ran advanced-{i}: struct={struct['total']}/"
                f"{20 + 15 * len(HEADINGS)}  "
                f"deleg={metrics['delegations']} waves={metrics['waves']} "
                f"conc={metrics['max_concurrency']} depth={metrics['depth']} "
                f"types={metrics['types']} web={metrics['web_searches']} "
                f"grep={metrics['greps']} wr={metrics['writes']} "
                f"bash={metrics['bash']} sub_turns={metrics['sub_turns']} "
                f"wall={metrics['seconds']:.1f}s",
                flush=True,
            )
            print(f"    chains: {' | '.join(chains) if chains else '(none)'}", flush=True)

        if not runs:
            print("  no runs completed — aborting", file=sys.stderr)
            return 1

        best = {
            "seconds": min(float(r["metrics"]["seconds"]) for r in runs),
            "sub_turns": min(float(r["metrics"]["sub_turns"]) for r in runs),
            "waves": min(float(r["metrics"]["waves"]) for r in runs),
        }
        for r in runs:
            r.update(_score_run(r, best))

        print(f"\n== advanced n={len(runs)} (v2 baseline: depth=1 total=85.7) ==")
        for r in sorted(runs, key=lambda x: int(x["run"])):
            m = r["metrics"]
            print(
                f"  run {r['run']}: total={r['total']:.1f}  A={r['A']:.1f} B={r['B']:.1f} "
                f"C={r['C']:.1f}  depth={m['depth']} types={m['types']} "
                f"deleg={m['delegations']} web={m['web_searches']} wr={m['writes']} "
                f"bash={m['bash']} wall={m['seconds']:.1f}s"
            )
        depths = [int(r["metrics"]["depth"]) for r in runs]
        print(f"\n  depth distribution: {sorted(depths)}")
        print(
            f"  total median: {_fmt_spread([r['total'] for r in runs])} /100"
            "  (v2 advanced baseline 85.7)"
        )
        if max(depths) >= 2:
            print("  >>> depth-2 FIRED: the coordinator chained a level-2 subagent.")
        else:
            print("  >>> depth-2 did NOT fire in this run.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"DEPTH-2 EXPERIMENT FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("--- last server log lines ---", file=sys.stderr)
        for line in server_log:
            print(line, file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        COORDINATOR_YAML.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
