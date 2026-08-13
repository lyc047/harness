"""Open-ended architecture-design benchmark, judged by a blind LLM.

Runs the same open-ended engineering task through normal / advanced / forced
depth-2 modes and writes report.md per run. Scoring is done by arch_judge.py.

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
    REPO_ROOT,
    _fmt_spread,
    _free_port,
    _wait_health,
)

RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))
GROUPS = ["normal", "advanced", "depth2"]
DELEGATE_PREFIX = "delegate_to_"
COORDINATOR_NAME = "coordinator"

TASK_PROMPT = """\
Design the system architecture for a realtime collaborative whiteboard platform
for teams — think a browser-based infinite canvas that many users edit together
live. Target: 1M daily active users, multi-region (US + EU + APAC), <200 ms
perceived latency for collaborative edits, 99.95% availability, a lean startup
budget. There is no single correct answer — we judge the quality of your
reasoning and trade-offs, not whether you guessed a specific stack.

Produce a report with EXACTLY these six sections, in this order:
1. Requirements & constraint analysis
2. Architecture overview
3. Subsystem designs
4. Key technology choices & rationale
5. Risks & failure scenarios
6. Evolution roadmap

In section 3, cover each of these eight subsystems with an explicit trade-off:
(a) storage & consistency, (b) realtime sync engine (WebSocket / CRDT-vs-OT
choice), (c) message fan-out & presence, (d) search & indexing, (e) auth &
tenant isolation, (f) observability, (g) multi-region & failover, (h) cost
control.

Write the report to {out}/report.md. Do not leave any section empty.
"""

COORDINATOR_YAML = REPO_ROOT / "skills" / "subagents" / f"{COORDINATOR_NAME}.yaml"

# The coordinator ships bundled (src/harness/skills/bundled/subagents/
# coordinator.yaml); the prompt above tells it how to run this report. No
# runtime write — a stale skills/subagents/coordinator.yaml from a killed run
# shadows the bundled set and breaks subagent-description tests.


def _prompt(out: str) -> str:
    return "Perform the following task.\n\n" + TASK_PROMPT.format(out=out)


def _prompt_depth2(out: str) -> str:
    body = TASK_PROMPT.format(out=out)
    return (
        "DELEGATE THE ENTIRE TASK to a single subagent — call the "
        f"delegate_to_{COORDINATOR_NAME} tool once and hand it the FULL task "
        "below plus the target report path. Do NOT research, read, search, or "
        "write anything yourself — the coordinator owns the whole job.\n\n"
        "NOTE: the coordinator has no write access. It is expected to hand the "
        "writing to the doc_writer subagent, which CAN write the file. Wait for "
        "the coordinator's summary and report back.\n\n"
        + body
    )


async def _run_mode(port: int, out: str, mode: str) -> dict[str, object]:
    """Run one run in normal/advanced/depth2; track delegation chain + tools."""
    import websockets

    prompt = _prompt_depth2(out) if mode == "depth2" else _prompt(out)
    advanced = mode != "normal"

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
        await ws.send(json.dumps({"type": "message", "content": prompt}))

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

    tmp = Path(tempfile.mkdtemp(prefix="harness-arch-"))
    runs: list[dict[str, Any]] = []
    try:
        _wait_health(port)
        for mode in GROUPS:
            for i in range(1, RUNS + 1):
                out_dir = tmp / f"{mode}-{i}"
                try:
                    metrics = asyncio.run(
                        asyncio.wait_for(_run_mode(port, str(out_dir), mode), timeout=900.0)
                    )
                except TimeoutError:
                    print(f"  {mode}-{i}: TIMEOUT after 900s — skipped", flush=True)
                    continue
                report = out_dir / "report.md"
                if not report.exists():
                    print(f"  {mode}-{i}: NO report.md — skipped", flush=True)
                    continue
                runs.append({"mode": mode, "out": str(out_dir), "metrics": metrics, "run": i})
                chains = [" -> ".join(c) for c in cast(list[list[str]], metrics["chain"])]
                print(
                    f"  ran {mode}-{i}: deleg={metrics['delegations']} "
                    f"waves={metrics['waves']} conc={metrics['max_concurrency']} "
                    f"depth={metrics['depth']} types={metrics['types']} "
                    f"web={metrics['web_searches']} bash={metrics['bash']} "
                    f"sub_turns={metrics['sub_turns']} wall={metrics['seconds']:.1f}s",
                    flush=True,
                )
                print(
                    f"    chains: {' | '.join(chains) if chains else '(none)'}",
                    flush=True,
                )
        if not runs:
            print("  no runs completed — aborting", file=sys.stderr)
            return 1

        from arch_judge import DIMENSIONS as JUDGE_DIMS
        from arch_judge import judge_report

        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        import random

        rng = random.Random(20260812)
        ordered = list(runs)
        rng.shuffle(ordered)  # deidentify: judge in mode-mixed random order
        for r in ordered:
            text = (Path(r["out"]) / "report.md").read_text(encoding="utf-8")
            r["judged"] = judge_report(text, model=model, samples=3)
            print(
                f"  judged {r['mode']}-{r['run']}: "
                f"total={r['judged'].get('total')}/60 "
                f"samples={r['judged'].get('samples')}",
                flush=True,
            )

        lines = ["# judge_results.md\n"]
        for r in sorted(runs, key=lambda x: (x["mode"], x["run"])):
            j = r["judged"]
            if "error" in j:
                lines.append(f"## {r['mode']}-{r['run']}\nERROR: {j['error']}\n")
                continue
            lines.append(f"## {r['mode']}-{r['run']}\n")
            for d in JUDGE_DIMS:
                lines.append(f"- {d}: {j['scores'][d]:g}/10  spread {j['spread'][d]}\n")
            lines.append(f"- TOTAL: {j['total']}/60 (samples {j['samples']})\n")
        out_md = REPO_ROOT / "scripts" / "arch_judge_results.md"
        out_md.write_text("".join(lines), encoding="utf-8")
        print(f"\njudge results written to {out_md}", flush=True)

        by_mode = {m: [r for r in runs if r["mode"] == m] for m in GROUPS}
        print("\n== LLM-judge comparison (medians of per-run medians) ==")
        for m in GROUPS:
            rs = [r for r in by_mode[m] if "judged" in r and "error" not in r["judged"]]
            if not rs:
                print(f"  {m}: no judged runs")
                continue
            totals = [float(r["judged"]["total"]) for r in rs]
            print(f"  {m:9s} n={len(rs)} total {_fmt_spread(totals)}/60")
            for d in JUDGE_DIMS:
                vals = [float(r["judged"]["scores"][d]) for r in rs]
                print(f"      {d:28s} {_fmt_spread(vals)}/10")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ARCH BENCH FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
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
