"""Multi-dimensional capability comparison: normal vs advanced orchestration.

Boots ONE web server (HARNESS_SUBAGENTS=1), runs the SAME auto-checkable task
RUNS times per mode against a real model — once with the advanced toggle off,
once on — and scores every run on three weighted dimension groups:

  A. Orchestration structure (25 pts) — max concurrency, delegation depth,
     subagent specialization. Evidence the mode's orchestration actually fired.
  B. Resource efficiency (30 pts)     — wall-clock, total subagent turns,
     delegation waves. Lower is better, normalized against the best run.
  C. Outcome quality (45 pts)         — deterministic factual checks: a
     structure gate, public-class coverage (AST-extracted), value precision
     against a curated gold fact set, and hallucination detection against
     verified-absent trap names. Zero LLM calls.

Per-mode aggregates are medians over RUNS with min–max spread. This is still a
DEMONSTRATION (a handful of real-model runs), not a scientific benchmark; but
the C dimension is deterministic and the A/B dimensions are structurally
determined by the mode, so direction is meaningful.

Exit codes:
    0  PASS (both modes completed; table printed)
    1  FAIL (a mode errored or scoring could not run)
    2  no API key configured
"""

from __future__ import annotations

import ast
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

# Six independent modules — a task that pressures a single parent context and
# naturally decomposes into parallel sub-researches. The rubric generalizes to
# whatever modules are listed here.
MODULES = [
    "src/harness/core/runner.py",
    "src/harness/safety/approver.py",
    "src/harness/planning/planner.py",
    "src/harness/agents/orchestrator.py",
    "src/harness/web/runtime.py",
    "src/harness/tools/mcp/client.py",
]
HEADINGS = [f"## {Path(m).stem.capitalize()}" for m in MODULES]
MIN_SECTION_CHARS = 200

# Runs per mode. Override with HARNESS_COMPARE_RUNS for quick trials.
RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))

# Delegation tool calls are named ``delegate_to_<subagent>``.
DELEGATE_PREFIX = "delegate_to_"

# ---- C3: curated precise facts (report must contain the exact literal) ----
# ``approver.enum`` requires ALL FOUR Mode members via lookaheads.
GOLD_FACTS: list[tuple[str, re.Pattern[str]]] = [
    ("runner.concurrent", re.compile(r"concurrent\s*=\s*False")),
    ("runner.resume", re.compile(r"resume_streamed")),
    ("runner.maxturns", re.compile(r"MaxTurnsExceeded")),
    (
        "approver.enum",
        re.compile(r"(?=.*\bPLAN\b)(?=.*\bASK\b)(?=.*\bAUTO\b)(?=.*\bFULL\b)"),
    ),
    ("approver.set_mode", re.compile(r"set_mode")),
    ("planner.max_steps", re.compile(r"max_steps\s*=\s*8")),
    ("planner.retries", re.compile(r"retries\s*=\s*2")),
    ("planner.extract_json", re.compile(r"extract_json")),
    ("orchestrator.budget", re.compile(r"SubagentBudget")),
    ("orchestrator.expected", re.compile(r"expected_output")),
    ("runtime.set_advanced", re.compile(r"set_advanced")),
    ("runtime.build", re.compile(r"build_runtime")),
    ("client.config", re.compile(r"MCPServerConfig")),
    ("client.add", re.compile(r"add_server")),
]

# ---- C4: plausible-but-fake API names; verified ABSENT from src/ ----
TRAP_NAMES = [
    "run_parallel",
    "ApprovalPolicy",
    "force_json",
    "delegate_once",
    "WebRunner",
    "MCPRegistry",
]


def _prompt(out: str) -> str:
    heading_list = ", ".join(f"'{h}'" for h in HEADINGS)
    return (
        f"Research the following {len(MODULES)} modules and write a markdown "
        f"report: {', '.join(MODULES)}.\n"
        f"The report must:\n"
        f"  (1) exist at {out}/report.md;\n"
        f"  (2) contain the {len(HEADINGS)} headings {heading_list}, "
        f"with a section describing each module;\n"
        f"  (3) each section at least {MIN_SECTION_CHARS} characters;\n"
        f"  (4) end with a '## Sources' section listing the {len(MODULES)} file paths.\n"
        "Be precise: for each module, name its public classes and key public "
        "functions with their exact signatures, default parameter values, and "
        "any enum members (e.g. `concurrent=False`, `max_steps=8`).\n"
        "This is a large research task — delegate independent sub-researches to "
        "the delegate_to_researcher tool, and when several sub-researches are "
        "independent, issue them in ONE response so they can run in parallel. "
        "Read each file before writing about it."
    )


def _public_classes(path: Path) -> list[str]:
    """Top-level public classes of a module (C2 coverage target)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef) and not n.name.startswith("_")
    ]


def _structure_score(out_dir: Path) -> dict[str, int]:
    """Deterministic completion gate: report exists / headings / lengths."""
    report = out_dir / "report.md"
    if not report.exists():
        return {"report": 0, "sections": 0, "length": 0, "total": 0}
    text = report.read_text(encoding="utf-8", errors="replace")

    sections = 0
    for heading in HEADINGS:
        if heading in text:
            sections += 1

    length_hits = 0
    body = re.split(r"^## .*$", text, flags=re.M)[1:]
    for chunk in body:
        if len(chunk.strip()) >= MIN_SECTION_CHARS:
            length_hits += 1

    return {
        "report": 20 if report.exists() else 0,
        "sections": sections,
        "length": min(length_hits, len(HEADINGS)),
        "total": 20 + 15 * sections + 10 * min(length_hits, len(HEADINGS)),
    }


def _factual_score(text: str) -> dict[str, float]:
    """Deterministic factual checks: class coverage / gold precision / traps."""
    classes = [n for m in MODULES for n in _public_classes(REPO_ROOT / m)]
    matched = sum(1 for c in classes if re.search(rf"\b{re.escape(c)}\b", text))
    coverage = matched / len(classes) if classes else 0.0

    gold_hits = sum(1 for _, pat in GOLD_FACTS if pat.search(text))
    precision = gold_hits / len(GOLD_FACTS)

    trap_hits = sum(1 for t in TRAP_NAMES if re.search(rf"\b{re.escape(t)}\b", text))
    clean = 1.0 - trap_hits / len(TRAP_NAMES)

    return {
        "coverage": coverage,
        "precision": precision,
        "clean": clean,
        "classes": matched,
        "total_classes": len(classes),
        "gold": gold_hits,
        "total_gold": len(GOLD_FACTS),
        "traps_hit": trap_hits,
    }


async def _run_mode(port: int, out: str, advanced: bool) -> dict[str, float]:
    """Run one mode against the real model; collect orchestration metrics.

    Depth is computed from WHO issued the delegation: a ``delegate_to_*`` tool
    call inside subagent run X's event stream makes the next ``subagent_start``
    a child of X — so concurrent siblings are NOT mistaken for nesting.
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
        if advanced:
            await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
            assert json.loads(await ws.recv())["type"] == "advanced_changed"
        await ws.send(json.dumps({"type": "message", "content": _prompt(out)}))

        started = time.monotonic()
        active = 0
        max_concurrency = 0
        depth_by_run: dict[str, int] = {}
        pending_delegator: str | None = None
        types: set[str] = set()
        sub_turns = 0
        waves = 0
        last_was_delegate_call = False
        parent_tool_calls = 0

        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "tool_call":  # parent's own tool call
                parent_tool_calls += 1
                name = frame["tool_call"]["name"]
                if name.startswith(DELEGATE_PREFIX):
                    if not last_was_delegate_call:
                        waves += 1  # one burst of delegates per parent response
                    last_was_delegate_call = True
                    pending_delegator = "root"
                else:
                    last_was_delegate_call = False
            elif t == "subagent_event":
                ev = frame["event"]
                if ev.get("type") == "tool_call" and ev["tool_call"]["name"].startswith(
                    DELEGATE_PREFIX
                ):
                    pending_delegator = frame["run_id"]
                last_was_delegate_call = False
            elif t == "subagent_start":
                types.add(frame["agent"])
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
                return {
                    "seconds": time.monotonic() - started,
                    "delegations": len(depth_by_run),
                    "waves": waves,
                    "max_concurrency": max_concurrency,
                    "depth": max(depth_by_run.values(), default=0),
                    "types": len(types),
                    "sub_turns": sub_turns,
                    "parent_tool_calls": parent_tool_calls,
                }
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")
            else:
                last_was_delegate_call = False


def _score_run(
    r: dict[str, object], best: dict[str, float]
) -> dict[str, float]:
    """Weighted composite for one run: A (25) + B (30) + C (45)."""
    m = r["metrics"]
    assert isinstance(m, dict)
    out_dir = r["out"]
    assert isinstance(out_dir, Path)

    # A — orchestration structure
    a1 = min(float(m["max_concurrency"]), 4.0) / 4.0 * 10.0
    depth = int(m["depth"])
    a2 = 8.0 if depth >= 2 else (4.0 if depth == 1 else 0.0)
    ntypes = int(m["types"])
    a3 = 7.0 if ntypes >= 2 else (3.5 if ntypes == 1 else 0.0)
    A = a1 + a2 + a3

    # B — resource efficiency (relative to the best run across ALL runs)
    b1 = best["seconds"] / float(m["seconds"]) * 15.0
    sub = float(m["sub_turns"])
    b2 = best["sub_turns"] / sub * 8.0 if sub > 0 else 0.0
    wav = float(m["waves"])
    b3 = best["waves"] / wav * 7.0 if wav > 0 else 0.0
    B = b1 + b2 + b3

    # C — outcome quality (gate: full completion, else C = 0)
    struct = _structure_score(out_dir)
    text = ""
    report = out_dir / "report.md"
    if report.exists():
        text = report.read_text(encoding="utf-8", errors="replace")
    gate = struct["total"] >= 20 + 15 * len(MODULES)
    if gate:
        f = _factual_score(text)
        C = f["coverage"] * 18.0 + f["precision"] * 18.0 + f["clean"] * 9.0
    else:
        f = {"coverage": 0.0, "precision": 0.0, "clean": 0.0, "traps_hit": 0}
        C = 0.0

    return {
        "A": A,
        "B": B,
        "C": C,
        "total": A + B + C,
        "gate": 1.0 if gate else 0.0,
        "facts": f,  # type: ignore[dict-item]
    }


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


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _fmt_spread(vals: list[float]) -> str:
    return f"{_median(vals):.1f} ({min(vals):.1f}–{max(vals):.1f})"


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
    runs: list[dict] = []
    try:
        _wait_health(port)
        for mode, advanced in (("normal", False), ("advanced", True)):
            for i in range(1, RUNS + 1):
                out_dir = tmp / f"{mode}-{i}"
                metrics = asyncio.run(
                    asyncio.wait_for(_run_mode(port, str(out_dir), advanced), timeout=300.0)
                )
                runs.append(
                    {"mode": mode, "out": out_dir, "metrics": metrics, "run": i}
                )
                struct = _structure_score(out_dir)
                print(
                    f"  ran {mode}-{i}: struct={struct['total']}/"
                    f"{20 + 15 * len(MODULES)}  "
                    f"deleg={metrics['delegations']} waves={metrics['waves']} "
                    f"conc={metrics['max_concurrency']} depth={metrics['depth']} "
                    f"types={metrics['types']} sub_turns={metrics['sub_turns']} "
                    f"wall={metrics['seconds']:.1f}s"
                )

        best = {
            "seconds": min(float(r["metrics"]["seconds"]) for r in runs),
            "sub_turns": min(float(r["metrics"]["sub_turns"]) for r in runs),
            "waves": min(float(r["metrics"]["waves"]) for r in runs),
        }
        for r in runs:
            r.update(_score_run(r, best))

        for mode in ("normal", "advanced"):
            rs = [r for r in runs if r["mode"] == mode]
            print(f"\n== {mode} (n={len(rs)}) ==")
            print(f"  A structure  {_fmt_spread([r['A'] for r in rs])} /25")
            print(f"  B resource   {_fmt_spread([r['B'] for r in rs])} /30")
            print(f"  C outcome    {_fmt_spread([r['C'] for r in rs])} /45")
            print(f"  total        {_fmt_spread([r['total'] for r in rs])} /100")
            facts = [r["facts"] for r in rs]
            if facts:
                cov = [f["coverage"] * 100 for f in facts]
                prec = [f["precision"] * 100 for f in facts]
                traps = [f["traps_hit"] for f in facts]
                print(
                    f"    coverage {_fmt_spread(cov)}%  precision {_fmt_spread(prec)}%  "
                    f"traps_hit {min(traps)}–{max(traps)}"
                )

        nt = _median([r["total"] for r in runs if r["mode"] == "normal"])
        at = _median([r["total"] for r in runs if r["mode"] == "advanced"])
        print(f"\nverdict: total median  normal={nt:.1f}  advanced={at:.1f}  delta={at - nt:+.1f}")
        for dim, key in (("A structure", "A"), ("B resource", "B"), ("C outcome", "C")):
            nv = _median([r[key] for r in runs if r["mode"] == "normal"])
            av = _median([r[key] for r in runs if r["mode"] == "advanced"])
            print(f"          {dim:11s} normal={nv:5.1f} advanced={av:5.1f} delta={av - nv:+.1f}")
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
