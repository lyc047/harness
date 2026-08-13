"""Pomodoro micro-sprint benchmark: normal vs forced-normal vs forced-advanced.

Each run has the model implement a single-user Pomodoro timer in a scratch dir
(engine / storage / api / static / tests / README). After the run the harness
copies scripts/pomodoro_verify_template.py into the dir as verify_impl.py and
runs it (primary axis: verify_pass 0-5), then runs the model's own pytest
(secondary metric). WS frames track the delegation chain / concurrency.

Env:
  HARNESS_COMPARE_RUNS      runs per group (default 3)
  HARNESS_COMPARE_GROUPS    comma-separated group labels to run (default all)

Exit codes: 0 ok, 1 fail, 2 no API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
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
# Single-agent (normal) runs of the full 10-file task routinely take 15-25 min
# with a slow model; the delegated smoke run finished in ~8 min because the
# subagents worked in parallel. 1800s gives single-agent runs room to finish,
# and the salvage path below still records verify/pytest if one overruns.
RUN_TIMEOUT = float(os.environ.get("HARNESS_COMPARE_TIMEOUT", "1800"))
# Every completed/salvaged run is appended here as JSONL so a process death
# (sleep/reboot/network flap) only costs the one in-flight run — a relaunch
# loads prior records and resumes from where it left off.
RESULTS_FILE = Path(tempfile.gettempdir()) / "harness-pomo-results.jsonl"
DELEGATE_PREFIX = "delegate_to_"


class _RunFailed(Exception):
    """A single run failed unrecoverably (e.g. the LLM API connection dropped).

    The benchmark must continue with the remaining runs, salvaging whatever
    files the failed run produced, rather than aborting everything.
    """
COORDINATOR_NAME = "coordinator"
VERIFY_TEMPLATE = REPO_ROOT / "scripts" / "pomodoro_verify_template.py"

# (label, forced, advanced) — only `advanced` varies between groups.
GROUPS_ALL: list[tuple[str, bool, bool]] = [
    ("normal", False, False),
    ("forced-normal", True, False),
    ("forced-advanced", True, True),
]
_filter = [s for s in os.environ.get("HARNESS_COMPARE_GROUPS", "").split(",") if s]
GROUPS = [g for g in GROUPS_ALL if not _filter or g[0] in _filter]

SPRINT_TASK = """\
Implement a complete single-user Pomodoro timer service in the directory {out}.
Use ONLY the Python standard library — no third-party imports. Do NOT run ruff
(this scratch dir has no ruff config). Write every file to {out} exactly.

You must produce these files, with these EXACT module contracts:

{out}/engine.py
  class PomodoroEngine(work_minutes: int = 25, break_minutes: int = 5,
                       clock: Callable[[], float] = time.monotonic)
    start() -> None           # idle -> work
    pause() -> None           # work|break -> paused, freezes elapsed_seconds
    resume() -> None          # paused -> work|break
    reset() -> None           # any -> idle, elapsed_seconds resets to 0
    state() -> str            # "idle" | "work" | "break" | "paused"
    elapsed_seconds() -> float
  `clock` must be injectable (default time.monotonic) so a test can fake time.
  A transition that does not apply to the current state is a NO-OP and must
  never raise (e.g. pause() while idle). After `work_minutes` of accumulated
  work the state becomes "break"; after `break_minutes` of break it returns to
  "work". elapsed_seconds() is frozen while paused.

{out}/storage.py
  class SessionStore(path: str | Path)
    create(duration_s: int, started_at: float, note: str = "") -> int   # new id
    get(session_id: int) -> dict | None
    list() -> list[dict]
    update(session_id: int, **fields: object) -> bool   # False if id missing
    delete(session_id: int) -> bool                      # False if id missing
  SQLite-backed and persistent across close-and-reopen. ALL SQL must use
  parameter binding (`?` placeholders) — never build SQL by f-string or string
  concatenation. Must be safe under concurrent create() calls from multiple
  threads (a lock + check_same_thread=False, or a connection per call).

{out}/api.py
  def create_server(store, static_dir, host="127.0.0.1", port=0) -> HTTPServer
  Endpoints:
    GET  /                  -> serve static/index.html from static_dir
    GET  /api/sessions      -> 200 + JSON list
    POST /api/sessions      -> body {"duration_s": int, "note": str}
                                201 + {"id": N}; 400 on malformed JSON or a
                                missing/non-positive duration_s
    GET  /api/sessions/<id> -> 200 + session dict; 404 if missing
  Reject request bodies over 64 KB with 413 or 400 WITHOUT crashing the server.
  Never return a 500 with a stack trace for malformed input.

{out}/static/index.html, {out}/static/app.js, {out}/static/style.css
  A working Pomodoro timer page. index.html references style.css and app.js,
  shows a timer display, and has Start / Pause / Reset <button> controls.
  app.js defines functions startTimer, pauseTimer, resetTimer and calls
  fetch("/api/sessions") (e.g. to POST a completed session). style.css has at
  least 15 rules and is linked from index.html.

{out}/test_engine.py, {out}/test_storage.py, {out}/test_api.py
  Your own pytest tests for the three modules. They must pass with:
  `uv run pytest -q {out}`

{out}/README.md
  Sections: Overview, Run, API, Tests. Include the command to start the server.

Security requirements: no eval()/exec() anywhere; no hardcoded passwords,
secrets, or API keys in any file.

When done, report the files you wrote and the pytest result.
"""

SPRINT_TASK_FORCED = (
    "DELEGATE THE ENTIRE TASK to a single subagent — call the "
    f"delegate_to_{COORDINATOR_NAME} tool once and hand it the FULL task below "
    "plus the target directory {out}. Do NOT scaffold, write, read, search, or "
    "implement anything yourself — the coordinator owns the whole job.\n\n"
    "NOTE: the coordinator cannot write files or run bash. It is expected to "
    "split the work and hand each piece to the matching subagents — coder for "
    "engine.py/storage.py/api.py and their tests, frontend_design for static/, "
    "security_reviewer for a read-only audit of storage.py and api.py, "
    "doc_writer for README.md — via its delegate_to_* tools, several in "
    "parallel where possible, then verify the result. Wait for the coordinator's "
    "summary and report back.\n\n"
    + SPRINT_TASK
)

# The coordinator now ships as a tracked bundled subagent config
# (src/harness/skills/bundled/subagents/coordinator.yaml), discovered alongside
# the other default subagents. Benchmarks no longer write a runtime override
# into the gitignored skills/subagents/ dir — a stale file left there by a
# killed run shadowed the bundled set and broke subagent-description tests.
COORDINATOR_YAML = REPO_ROOT / "skills" / "subagents" / f"{COORDINATOR_NAME}.yaml"


def _salvage_run(
    label: str, i: int, out_dir: Path, *, reason: str, seconds: float
) -> dict[str, Any]:
    """Record a run that ended without a clean run_done (timeout / API failure).

    WS-derived metrics are unknown; verify/pytest are computed from whatever
    files exist so a slow-but-complete agent is not wasted.
    """
    verify_pass = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    print(
        f"  salvaged {label}-{i}: verify={verify_pass}/5 pytest={pytest_passed}",
        flush=True,
    )
    return {
        "mode": label,
        "out": str(out_dir),
        "metrics": {
            "seconds": seconds,
            "delegations": 0,
            "waves": 0,
            "max_concurrency": 0,
            "depth": 0,
            "types": 0,
            "sub_turns": 0,
            "web_searches": 0,
            "greps": 0,
            "writes": 0,
            "bash": 0,
            "chain": [],
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
        },
        "run": i,
        "reason": reason,
    }


def _prompt(out: str) -> str:
    # NOTE: SPRINT_TASK contains literal JSON braces ({"duration_s": ...}) so
    # str.format() would try to parse them as replacement fields — use replace.
    return "Perform the following task.\n\n" + SPRINT_TASK.replace("{out}", out)


def _prompt_forced(out: str) -> str:
    return "Perform the following task.\n\n" + SPRINT_TASK_FORCED.replace("{out}", out)


def _run_verify(out_dir: Path) -> int:
    """Copy the gate into {out} and run it; return verify_pass (0-5)."""
    (out_dir / "verify_impl.py").write_text(
        VERIFY_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    try:
        proc = subprocess.run(
            ["uv", "run", "python", str(out_dir / "verify_impl.py")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        print("    verify: TIMEOUT", flush=True)
        return 0
    for line in proc.stdout.splitlines():
        print("    " + line, flush=True)
    m = re.search(r"VERIFY_PASS (\d)/5", proc.stdout)
    return int(m.group(1)) if m else 0


def _run_pytest(out_dir: Path) -> int:
    """Run the model's own tests; return passed count (secondary metric)."""
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", "-q", str(out_dir)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("    pytest: TIMEOUT", flush=True)
        return 0
    tail = proc.stdout.strip().splitlines()
    if tail:
        print(f"    pytest: {tail[-1]}", flush=True)
    m = re.search(r"(\d+) passed", proc.stdout + proc.stderr)
    return int(m.group(1)) if m else 0


async def _run_mode(port: int, out: str, *, prompt: str, advanced: bool) -> dict[str, object]:
    """Run one pomodoro run; track delegation chain + tool use + verify/pytest."""
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
                break
            elif t == "run_error":
                raise _RunFailed(frame.get("message", "run_error"))
            else:
                last_was_delegate_call = False

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
        out_dir = Path(out)
        verify_pass = _run_verify(out_dir)
        pytest_passed = _run_pytest(out_dir)
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
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
        }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2
    if not VERIFY_TEMPLATE.is_file():
        print(f"missing {VERIFY_TEMPLATE} — run Task 3 first", file=sys.stderr)
        return 1

    port = _free_port()
    env = {
        **os.environ,
        "HARNESS_SUBAGENTS": "1",
        "HARNESS_SUBAGENT_BUDGET": "120",
    }
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

    tmp = Path(tempfile.mkdtemp(prefix="harness-pomo-"))
    runs: list[dict[str, Any]] = []
    done: set[tuple[str, int]] = set()
    if RESULTS_FILE.is_file():
        for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            done.add((rec["mode"], rec["run"]))
            runs.append(rec)
        print(
            f"resume: {len(done)} runs already recorded in {RESULTS_FILE} — "
            "skipping them",
            flush=True,
        )
    try:
        _wait_health(port)
        for label, forced, advanced in GROUPS:
            for i in range(1, RUNS + 1):
                if (label, i) in done:
                    continue
                out_dir = tmp / f"{label}-{i}"
                out_dir.mkdir(parents=True, exist_ok=True)
                prompt = _prompt_forced(str(out_dir)) if forced else _prompt(str(out_dir))
                try:
                    metrics = asyncio.run(
                        asyncio.wait_for(
                            _run_mode(port, str(out_dir), prompt=prompt, advanced=advanced),
                            timeout=RUN_TIMEOUT,
                        )
                    )
                    record = {
                        "mode": label,
                        "out": str(out_dir),
                        "metrics": metrics,
                        "run": i,
                    }
                    chains = [" -> ".join(c) for c in cast(list[list[str]], metrics["chain"])]
                    print(
                        f"  ran {label}-{i}: verify={metrics['verify_pass']}/5 "
                        f"pytest={metrics['pytest_passed']} deleg={metrics['delegations']} "
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
                except TimeoutError:
                    print(
                        f"  {label}-{i}: TIMEOUT after {RUN_TIMEOUT:.0f}s — salvaging",
                        flush=True,
                    )
                    record = _salvage_run(
                        label, i, out_dir, reason="timeout", seconds=RUN_TIMEOUT
                    )
                except _RunFailed as exc:
                    print(f"  {label}-{i}: run failed ({exc}) — salvaging", flush=True)
                    record = _salvage_run(
                        label, i, out_dir, reason=f"run_failed: {exc}", seconds=0.0
                    )
                runs.append(record)
                with RESULTS_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if not runs:
            print("  no runs completed — aborting", file=sys.stderr)
            return 1

        by_mode = {g[0]: [r for r in runs if r["mode"] == g[0]] for g in GROUPS}
        print("\n== pomodoro-sprint comparison (verify_pass 0-5 = primary) ==")
        for label, _forced, _adv in GROUPS:
            rs = by_mode[label]
            if not rs:
                print(f"  {label:15s} n=0 (no completed runs)")
                continue
            vps = [float(r["metrics"]["verify_pass"]) for r in rs]
            pps = [float(r["metrics"]["pytest_passed"]) for r in rs]
            walls = [float(r["metrics"]["seconds"]) for r in rs]
            depths = [float(r["metrics"]["depth"]) for r in rs]
            concs = [float(r["metrics"]["max_concurrency"]) for r in rs]
            print(
                f"  {label:15s} n={len(rs)}  verify {_fmt_spread(vps)}/5  "
                f"pytest {_fmt_spread(pps)}"
            )
            print(
                f"      wall {_fmt_spread(walls)}s  depth {_fmt_spread(depths)}  "
                f"conc {_fmt_spread(concs)}"
            )
        print("\n== delegation chains (deduped per group) ==")
        for label, _forced, _adv in GROUPS:
            chains: set[str] = set()
            for r in by_mode[label]:
                for c in r["metrics"]["chain"]:
                    chains.add(" -> ".join(c))
            print(
                f"  {label:15s} "
                + (" | ".join(sorted(chains)) if chains else "(none)")
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"POMO BENCH FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
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
