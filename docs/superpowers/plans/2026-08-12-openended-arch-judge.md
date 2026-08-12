# Open-Ended Architecture-Judge Benchmark — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/e2e_subagents_compare_v4.py` (plus a standalone judge module `scripts/arch_judge.py`) that runs an open-ended, no-correct-answer architecture-design task through all three modes (normal / advanced / forced depth-2) and grades the reports with a blind, multi-sample LLM judge — surfacing differences in problem-solving capability.

**Architecture:** Reuse the proven v3 WS driver for report generation; add a deidentified judge that calls the same DeepSeek model 3× per report and reports per-dimension median + variance. Generation and judging are separated into two modules so the judge is testable offline without a live model.

**Tech Stack:** Python 3.11, existing WS frames, `openai` (OpenAI-compatible, `base_url=https://api.deepseek.com`, model default `deepseek-v4-flash`), stdlib `re`/`random`/`statistics`.

## Global Constraints

- **Never print, log, or echo any API key.** The judge reads `DEEPSEEK_API_KEY` from `.env` via `load_dotenv` and passes it to `OpenAI(...)` — the key never appears in any output, log line, or report.
- Judge input is **deidentified**: only the fixed task text + the report body; no mode label, no run number, no script path.
- Judge uses the **same model as generation**: `os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")` (same default the harness uses). Report this in the summary; never present scores as absolute truth.
- `MAX_JUDGE_CHARS = 8000` hard cap on the report body passed to the judge.
- Two-phase execution: `HARNESS_COMPARE_RUNS=1` smoke first (all 3 groups), then `HARNESS_COMPARE_RUNS=3`.
- Quality gate per task: `uv run ruff check scripts/e2e_subagents_compare_v4.py scripts/arch_judge.py && uv run python -m py_compile scripts/e2e_subagents_compare_v4.py scripts/arch_judge.py && uv run pytest tests/test_arch_judge.py -q`.
- **No changes to `src/harness/**`.**
- 6 mandated report sections and 8 required subsystem subsections are the comparability contract — they must be copied into `TASK_PROMPT` verbatim.

---

### Task 1: v4 script — three-mode report generator (reuses v3 WS driver)

**Files:**
- Create: `scripts/e2e_subagents_compare_v4.py`
- Read first: `scripts/e2e_subagents_compare_v3.py` (the WS driver to adapt) and `scripts/e2e_subagents_compare_v2.py` (exports `REPO_ROOT`, `_free_port`, `_fmt_spread`, `_wait_health`)

**Interfaces:**
- Consumes: `_wait_health(port)`, `_free_port()`, `_fmt_spread(vals)`, `REPO_ROOT` from `scripts/e2e_subagents_compare_v2`; the WS frame shapes from v3 (`ready`, `set_advanced`, `message`, `subagent_start`, `subagent_event`, `subagent_end`, `approval_required`, `run_done`).
- Produces: `_run_mode(port, out, mode) -> dict[str, object]` returning metrics with keys `seconds`, `delegations`, `waves`, `max_concurrency`, `depth`, `types`, `sub_turns`, `web_searches`, `greps`, `writes`, `bash`, `chain` — the exact shape Task 2's judge driver consumes.

**Goal:** The generator half: run `TASK_PROMPT` (open-ended whiteboard architecture) through `normal`, `advanced`, and `depth2` modes, writing `report.md` per run. No scoring yet.

- [ ] **Step 1: Create the script with constants.**

Copy this header block verbatim (from the spec — these are the comparability contract):

```python
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
```

- [ ] **Step 2: Add the coordinator override (adapted from v3).**

```python
COORDINATOR_YAML = (
    REPO_ROOT / "skills" / "subagents" / f"{COORDINATOR_NAME}.yaml"
)
COORDINATOR_INSTRUCTIONS = """\
You are a research coordinator. You own the whole task end to end.

YOUR TOOLS: you can read files, glob, grep, and web_search. You CANNOT write
files and CANNOT run bash — you have no write_file or bash tool at all.

When the deliverable must be written to a file, delegate the writing to the
doc_writer subagent via the delegate_to_doc_writer tool: hand it your findings
plus the target path, and let IT save the file. Do NOT ask the parent to write
it — you drive the work.

When you finish, return a short summary: what you researched, what was written,
and the exact file path where the report now lives.
"""

COORDINATOR_YAML_TEXT = (
    "name: " + COORDINATOR_NAME + "\n"
    "description: Use when a whole multi-step task should run as one coordinated "
    "job — it researches and hands the final write-off to the doc_writer subagent.\n"
    "instructions: |\n"
    + "".join("  " + line + "\n" for line in COORDINATOR_INSTRUCTIONS.splitlines())
    + 'model: ""\n'
    "max_turns: 12\n"
    "tools:\n"
    "  - read_file\n"
    "  - glob_files\n"
    "  - grep_files\n"
    "  - web_search\n"
)


def _write_coordinator() -> None:
    COORDINATOR_YAML.parent.mkdir(parents=True, exist_ok=True)
    COORDINATOR_YAML.write_text(COORDINATOR_YAML_TEXT, encoding="utf-8")
```

- [ ] **Step 3: Two prompt builders.**

`_prompt(out: str) -> str` = `f"Perform the following task. {TASK_PROMPT.format(out=out)}"`. (Use `TASK_PROMPT` with the `{out}` placeholder filled.)

`_prompt_depth2(out: str) -> str` = the same body, prefixed with the forced-delegation instruction (from v3, reworded for a design task):

```python
def _prompt_depth2(out: str) -> str:
    body = TASK_PROMPT.format(out=out)
    return (
        "DELEGATE THE ENTIRE TASK to a single subagent — call the "
        f"delegate_to_{COORDINATOR_NAME} tool once and hand it the FULL task "
        "below plus the target report path. Do NOT research, read, search, or "
        "write anything yourself — the coordinator owns the whole job.\n\n"
        f"NOTE: the coordinator has no write access. It is expected to hand the "
        f"writing to the doc_writer subagent, which CAN write the file. Wait for "
        f"the coordinator's summary and report back.\n\n{body}"
    )
```

- [ ] **Step 4: Adapt `_run_mode` from v3** (`scripts/e2e_subagents_compare_v3.py` lines 110-216). Read it first; copy it and change exactly this:

```python
async def _run_mode(port: int, out: str, mode: str) -> dict[str, object]:
    """Run one run in normal/advanced/depth2; track delegation chain + tools."""
    prompt = _prompt_depth2(out) if mode == "depth2" else _prompt(out)
    advanced = mode != "normal"
    # ... v3 body unchanged from here, except:
    #   if advanced:  (was `if advanced:` — same, since advanced == (mode != "normal"))
    #   await ws.send({"type":"message","content": prompt})  (was _prompt(out))
```

The return dict is byte-identical to v3's (seconds, delegations, waves, max_concurrency, depth, types, sub_turns, web_searches, greps, writes, bash, chain). Note: `_run_mode`'s `advanced` variable now derives from `mode`, so no signature change ripples into the body.

- [ ] **Step 5: `main()` — three-group runner (generation only).**

```python
def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2

    _write_coordinator()
    port = _free_port()
    env = {**os.environ, "HARNESS_SUBAGENTS": "1"}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "harness.web.server:create_app",
            "--factory", "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
                    metrics = asyncio.run(asyncio.wait_for(
                        _run_mode(port, str(out_dir), mode), timeout=900.0))
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
                print(f"    chains: {' | '.join(chains) if chains else '(none)'}", flush=True)
        if not runs:
            print("  no runs completed — aborting", file=sys.stderr)
            return 1
        # Task 2 will hang scoring off this `runs` list. For Task 1, just report:
        by_mode = {m: [r for r in runs if r["mode"] == m] for m in GROUPS}
        for m in GROUPS:
            rs = by_mode[m]
            print(f"\n== {m} n={len(rs)} ==")
            print(f"  depths: {sorted(int(r['metrics']['depth']) for r in rs)}")
            print(f"  wall: {_fmt_spread([float(r['metrics']['seconds']) for r in rs])}s")
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
```

- [ ] **Step 6: Quality gate + smoke.**

Run `uv run ruff check scripts/e2e_subagents_compare_v4.py && uv run python -m py_compile scripts/e2e_subagents_compare_v4.py`.

Then smoke: `HARNESS_COMPARE_RUNS=1 uv run python scripts/e2e_subagents_compare_v4.py`. Expected: 3 runs (one per group), each printing a metrics line and `report.md` existing. Then verify section coverage offline:

```bash
for d in $(find "$(ls -d /tmp/harness-arch-* | tail -1)" -name report.md); do
  echo "== $d"; grep -cE "^[0-9]\. " "$d" || true
done
```

Expected: 6 headings per report (`1.`…`6.`). The exact `/tmp` path will differ — inspect the script's printed temp dir or use the newest `harness-arch-*`.

- [ ] **Step 7: Commit.**

```bash
git add scripts/e2e_subagents_compare_v4.py
git commit -m "bench: open-ended arch-design generator (3 modes) for LLM-judged comparison"
```

---

### Task 2: Standalone blind judge module + parser tests

**Files:**
- Create: `scripts/arch_judge.py`
- Create: `tests/test_arch_judge.py`

**Interfaces:**
- Consumes: nothing from Task 1 — a standalone CLI that reads `report.md` files from a directory.
- Produces: `_parse_judge_output(text: str) -> dict[str, Any] | None`; `_blind_render(text: str, max_chars: int = 8000) -> str`; `judge_report(report_text: str, *, model: str, samples: int = 3) -> dict[str, Any]`. Task 3's integration calls these.

**Goal:** The deidentified judge: blind-render a report, call the same DeepSeek model `samples` times, parse per-dimension scores, return medians + spread + total. Testable offline via the parser unit tests.

- [ ] **Step 1: Write the failing parser test** — `tests/test_arch_judge.py`:

```python
from arch_judge import DIMENSIONS, _blind_render, _parse_judge_output


def test_parse_judge_output_valid():
    text = (
        "1: 8/10 — solid constraints\n"
        "2: 7/10 — coherent\n"
        "3: 9/10 — good trade-offs\n"
        "4: 8/10 — complete\n"
        "5: 6/10 — vague\n"
        "6: 7/10 — risks covered\n"
        "TOTAL: 45/60\n"
    )
    out = _parse_judge_output(text)
    assert out is not None
    assert out["total"] == 45
    assert [out["scores"][d] for d in DIMENSIONS] == [8, 7, 9, 8, 6, 7]


def test_parse_judge_output_missing_dimension():
    text = "1: 8/10 — ok\n3: 9/10 — ok\nTOTAL: 17/60\n"
    assert _parse_judge_output(text) is None


def test_parse_judge_output_garbage():
    assert _parse_judge_output("not a judge output at all") is None


def test_blind_render_truncates():
    text = "x" * 20000
    assert len(_blind_render(text, max_chars=8000)) == 8000
```

- [ ] **Step 2: Run it to verify it fails** — `uv run pytest tests/test_arch_judge.py -q`. Expected: collection error (`no module named arch_judge`).

- [ ] **Step 3: Implement `scripts/arch_judge.py`.**

```python
"""Blind LLM judge for architecture-design benchmark reports.

Reads report.md files (deidentified: no mode/run labels), scores each with the
same DeepSeek model the generator used, N samples per report, per-dimension
median + spread. CLI: `python scripts/arch_judge.py <dir-with-report.md...>`.

Exit codes: 0 ok, 1 fail, 2 no API key.
"""

from __future__ import annotations

import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from e2e_subagents_compare_v2 import REPO_ROOT
from openai import OpenAI

DIMENSIONS = [
    "Requirements understanding",
    "Architectural soundness",
    "Trade-off awareness",
    "Completeness",
    "Deployability",
    "Risk & evolution",
]
MAX_JUDGE_CHARS = 8000

JUDGE_SYSTEM_PROMPT = """\
You are a principal systems architect grading a peer's architecture-design report.
Grade ONLY what is in the report. You do not know the author, the tool, or the mode.
Score each dimension 1-10 (integer) and give one short sentence of justification per
dimension, then a total (sum, max 60). Use exactly this format, one line per dimension:
1. Requirements understanding: <n>/10 — <one-sentence justification>
2. Architectural soundness: <n>/10 — <one-sentence justification>
3. Trade-off awareness: <n>/10 — <one-sentence justification>
4. Completeness: <n>/10 — <one-sentence justification>
5. Deployability: <n>/10 — <one-sentence justification>
6. Risk & evolution: <n>/10 — <one-sentence justification>
TOTAL: <sum>/60
"""

_DIM_LINE = re.compile(r"^[1-6]\.\s*[^:]*:\s*(\d+)/10")
_TOTAL_LINE = re.compile(r"^TOTAL:\s*(\d+)/60", re.IGNORECASE)


def _parse_judge_output(text: str) -> dict[str, Any] | None:
    """Parse judge text into {scores: {dim: int}, total: int}; None on bad format."""
    scores: dict[str, int] = {}
    total: int | None = None
    for line in text.splitlines():
        m = _DIM_LINE.match(line.strip())
        if m:
            scores[len(scores)] = int(m.group(1))
        m2 = _TOTAL_LINE.match(line.strip())
        if m2:
            total = int(m2.group(1))
    if len(scores) != len(DIMENSIONS) or total is None:
        return None
    return {"scores": dict(zip(DIMENSIONS, [scores[i] for i in range(len(DIMENSIONS))])), "total": total}


def _blind_render(text: str, max_chars: int = MAX_JUDGE_CHARS) -> str:
    """Deidentify + cap: just the body, truncated, no labels attached."""
    return text[:max_chars]


def _judge_once(rendered: str, *, model: str) -> dict[str, Any] | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": rendered},
        ],
        temperature=0.4,
        max_tokens=1000,
    )
    content = resp.choices[0].message.content or ""
    return _parse_judge_output(content)


def judge_report(report_text: str, *, model: str, samples: int = 3) -> dict[str, Any]:
    """Blind-judge one report `samples` times; return per-dim median/spread + total."""
    rendered = _blind_render(report_text)
    per_dim: list[dict[str, int]] = []
    totals: list[int] = []
    for _ in range(samples):
        parsed = _judge_once(rendered, model=model)
        if parsed is not None:
            per_dim.append(parsed["scores"])
            totals.append(parsed["total"])
    if not per_dim:
        return {"error": "no valid judge samples"}
    return {
        "scores": {
            d: statistics.median(s[d] for s in per_dim) for d in DIMENSIONS
        },
        "spread": {
            d: (min(s[d] for s in per_dim), max(s[d] for s in per_dim))
            for d in DIMENSIONS
        },
        "total": int(statistics.median(totals)),
        "samples": len(totals),
    }


def main(argv: list[str]) -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    ok = True
    for arg in argv:
        text = Path(arg).read_text(encoding="utf-8")
        result = judge_report(text, model=model)
        if "error" in result:
            ok = False
        print(f"== {arg}")
        for d in DIMENSIONS:
            print(f"  {d}: {result.get('scores', {}).get(d)}/10")
        print(f"  TOTAL: {result.get('total')}/60  samples={result.get('samples')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests + quality gate.**

`uv run pytest tests/test_arch_judge.py -q` (pass), then `uv run ruff check scripts/arch_judge.py tests/test_arch_judge.py && uv run python -m py_compile scripts/arch_judge.py`.

- [ ] **Step 5: Live judge smoke** — one real call to confirm parsing works end-to-end on a real model response:

```bash
uv run python scripts/arch_judge.py "$(find "$(ls -d /tmp/harness-arch-* 2>/dev/null | tail -1)" -name report.md 2>/dev/null | head -1)"
```

Expected: 6 dimension lines + `TOTAL: <n>/60` printed. If `_parse_judge_output` returns None (format drift), fix the regexes to match the model's actual output before proceeding.

- [ ] **Step 6: Commit.**

```bash
git add scripts/arch_judge.py tests/test_arch_judge.py
git commit -m "bench: blind LLM judge module for arch reports + parser tests"
```

---

### Task 3: Wire judge into v4 + full run + my review

**Files:**
- Modify: `scripts/e2e_subagents_compare_v4.py` (import + scoring block in `main`)
- Create: `scripts/arch_judge_results.md` (generated at run time, git-ignored later if noisy)

**Interfaces:**
- Consumes: `judge_report(report_text, *, model, samples)` and `DIMENSIONS` from `arch_judge.py` (Task 2).
- Produces: a per-group score table + `judge_results.md` (deidentified per-report medians), then the human review pass.

**Goal:** Connect scoring, run the full experiment, and produce the comparison the user asked for — including an honest uncertainty readout and my manual review of the most/least-separated pairs.

- [ ] **Step 1: Wire scoring into `main()`** — right after the generation loop in Task 1's `main` (after the `if not runs` guard), add:

```python
        from arch_judge import DIMENSIONS as JUDGE_DIMS, judge_report

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
```

Then the per-group summary table (replacing Task 1's depth-only block):

```python
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
```

- [ ] **Step 2: Quality gate + smoke (RUNS=1, real judge).**

`uv run ruff check scripts/e2e_subagents_compare_v4.py && uv run python -m py_compile scripts/e2e_subagents_compare_v4.py`, then `HARNESS_COMPARE_RUNS=1 uv run python scripts/e2e_subagents_compare_v4.py`. Expected: 3 runs generated, 3 judged, `scripts/arch_judge_results.md` written with non-error rows. If any row is `ERROR: no valid judge samples`, fix `_parse_judge_output` to match the live model output (see Task 2 Step 5).

- [ ] **Step 3: Full run (RUNS=3).**

`HARNESS_COMPARE_RUNS=3 uv run python scripts/e2e_subagents_compare_v4.py` (background, ~40-70 min). Expected: 9 runs, all judged, summary table with per-group total medians + per-dimension spreads.

- [ ] **Step 4: My manual review pass.**

I read `scripts/arch_judge_results.md` plus the raw reports for (a) the mode pair with the largest total gap and (b) the pair with the smallest. I sanity-check whether the judge's per-dimension verdicts match the actual report content, and I report that verification explicitly — including any case where the judge's score contradicts my reading.

- [ ] **Step 5: Report + commit.**

Write the final comparison summary (per-group total median, per-dimension deltas, depth/chain structure from the metrics, judge variance, my review pass) in Chinese. Commit any script fixes from the review as one commit:

```bash
git add scripts/ arch_judge_results.md
git commit -m "bench: full open-ended arch-judge run (normal/advanced/depth2) + results"
```

---

## Self-Review

**Spec coverage:** task prompt (Task 1 Step 1), coordinator override (Task 1 Step 2), judge rubric + blind render + 3-sample median/variance (Task 2), three groups + two-phase smoke/n=3 (Tasks 1/3), deidentification via mode-mixed shuffle (Task 3 Step 1), my review pass (Task 3 Step 4), cost/risk honest reporting (Task 3 Step 5). The 6 sections + 8 subsystems contract lives in `TASK_PROMPT` verbatim. API-key secrecy is a Global Constraint and encoded in both scripts (env-only, never echoed).

**Placeholder scan:** no TBD/TODO; every step has concrete code or an explicit "copy from v3, change exactly X" instruction. The v3 references name exact line ranges and the precise diffs, not vague reuse.

**Type consistency:** `_run_mode` returns the same 12-key metrics dict in Task 1 that Task 3 indexes (`metrics['depth']`, etc.); `judge_report` returns `{scores, spread, total, samples}` that Task 3 consumes (`judged['total']`, `judged['scores'][d]`, `judged['spread'][d]`, `judged['error']` guard). `DIMENSIONS` is imported under the alias `JUDGE_DIMS` everywhere it is used in Task 3.
