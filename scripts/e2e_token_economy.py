"""Token-economy benchmark: normal (pro only) vs forced-advanced (flash subagents).

Each run has the model implement the pomodoro sprint (same task/gates as
scripts/e2e_subagents_compare_v6.py). The benchmark runs **in-process**: a
fresh CoreStack per run with two usage-tracking provider instances (main=pro,
subagents=flash), so token usage is attributed per model exactly. After each
run it runs the verify gate, the model's own pytest, and the adversarial
robustness audit (scripts/score_robustness.py).

Primary claim: forced-advanced uses far fewer PRO tokens/context than normal,
with quality gates (verify 5/5, pytest, robustness) not degraded.

Env:
  HARNESS_COMPARE_RUNS      runs per group (default 3); also accepts
                            "normal=3,forced-advanced=5" for unequal counts
  HARNESS_COMPARE_GROUPS    comma-separated group labels (default
                            "normal,forced-advanced")
  HARNESS_COMPARE_TIMEOUT   per-run timeout seconds (default 1800)
  HARNESS_SUBAGENT_BUDGET   subagent turn budget (default 120)
  HARNESS_TOKEN_ECON_RESULTS  results JSONL path override (default: tempdir)

Exit codes: 0 ok, 1 fail, 2 no API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Run from the repo root so relative skills/ and data dirs resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))

from e2e_subagents_compare_v6 import (  # noqa: E402
    _prompt,
    _prompt_forced,
    _run_pytest,
    _run_verify,
)
from score_robustness import score as robust_score  # noqa: E402

from harness.agents.orchestrator import SubagentRunStart  # noqa: E402
from harness.config import Settings  # noqa: E402
from harness.core.compose import add_example_subagents, build_core_stack  # noqa: E402
from harness.core.messages import ToolCall  # noqa: E402
from harness.llm.openai_compat import OpenAICompatProvider  # noqa: E402

DEFAULT_RUNS = 3
RUN_TIMEOUT = float(os.environ.get("HARNESS_COMPARE_TIMEOUT", "1800"))
GROUPS_ALL = ("normal", "forced-advanced")
_filter = [s for s in os.environ.get("HARNESS_COMPARE_GROUPS", "").split(",") if s]
GROUPS = [g for g in GROUPS_ALL if not _filter or g in _filter]
# Per-group run counts: "5" (every group) or "normal=3,forced-advanced=5".
# Fixes the #7 sample-size complaint: advanced is the group that matters and
# is cheap per run, so it can afford more samples than pro-only normal runs.


def parse_runs(spec: str) -> dict[str, int]:
    """Parse HARNESS_COMPARE_RUNS into per-group run counts.

    ``"5"`` runs every group 5 times; ``"normal=3,forced-advanced=5"``
    overrides per group. Unknown groups or a malformed entry raise
    ``ValueError`` so a typo in the env fails loudly instead of silently
    running the default.
    """
    spec = (spec or "").strip()
    if not spec:
        return {}
    if "=" not in spec:
        n = int(spec)  # raises ValueError on garbage
        return {g: n for g in GROUPS_ALL}
    out: dict[str, int] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, n = part.partition("=")
        name, n = name.strip(), n.strip()
        if not sep or name not in GROUPS_ALL:
            raise ValueError(f"invalid HARNESS_COMPARE_RUNS entry: {part!r}")
        out[name] = int(n)
    return out


RUNS_SPEC = parse_runs(os.environ.get("HARNESS_COMPARE_RUNS", str(DEFAULT_RUNS)))
RESULTS_FILE = Path(
    os.environ.get(
        "HARNESS_TOKEN_ECON_RESULTS",
        str(Path(tempfile.gettempdir()) / "harness-token-econ-results.jsonl"),
    )
)

# Per-MTok USD pricing (cc-switch model_pricing table; matches DeepSeek
# published pricing — edit to match your billing).
PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"in": 1.68, "out": 3.36},
    "deepseek-v4-flash": {"in": 0.14, "out": 0.28},
}
SUBAGENT_MODEL = "deepseek-v4-flash"
SUBAGENT_BUDGET = int(os.environ.get("HARNESS_SUBAGENT_BUDGET", "120"))
# Gate-driven repair (#1): max fix-subagent rounds after a run that fails the
# verify gate / robustness audit. 0 (default) keeps the v2 benchmark behavior
# byte-for-byte identical — repair only activates when set explicitly via
# HARNESS_COMPARE_REPAIR.
REPAIR_MAX = int(os.environ.get("HARNESS_COMPARE_REPAIR", "0"))


# ---- pure helpers (imported by tests) ---- #


def sum_usage(records: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate usage records into {prompt, completion, reasoning, total}."""
    return {
        "prompt": sum(int(r.get("prompt_tokens", 0)) for r in records),
        "completion": sum(int(r.get("completion_tokens", 0)) for r in records),
        "reasoning": sum(int(r.get("reasoning_tokens", 0)) for r in records),
        "total": sum(
            int(r.get("prompt_tokens", 0)) + int(r.get("completion_tokens", 0))
            for r in records
        ),
    }


def cost(
    records: list[dict[str, Any]],
    pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """USD cost of usage records at the given per-MTok pricing."""
    pricing = pricing or PRICING
    total = 0.0
    for r in records:
        p = pricing.get(str(r.get("model", "")))
        if p is None:
            continue
        total += (int(r.get("prompt_tokens", 0)) / 1e6) * p["in"]
        total += (int(r.get("completion_tokens", 0)) / 1e6) * p["out"]
    return round(total, 6)


def pro_reduction(advanced_pro: list[float], normal_pro: list[float]) -> float | None:
    """Pro-token reduction = 1 - mean(advanced)/mean(normal); None if undefined."""
    if not normal_pro:
        return None
    base = sum(normal_pro) / len(normal_pro)
    if base <= 0:
        return None
    adv = sum(advanced_pro) / len(advanced_pro) if advanced_pro else 0.0
    return 1 - adv / base


def _fmt_stats(vals: list[float]) -> str:
    """mean (median) [min–max] ±std string (#7); 'n/a' when empty."""
    if not vals:
        return "n/a"
    m = sum(vals) / len(vals)
    med = statistics.median(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return f"{m:.1f} ({med:.1f}) [{min(vals):.1f}–{max(vals):.1f}] ±{sd:.1f}"


# ---- gate-driven repair helpers (#1) ---- #


# Verify gate name -> the file(s) that gate exercises. The repair brief points
# the fix subagent at exactly these paths instead of the whole out dir.
GATE_FILE = {
    "engine": "engine.py",
    "storage": "storage.py",
    "api": "api.py",
    "static": "static/",
    "readme": "README.md",
}
# Gate -> the subagent best equipped to fix it; everything else falls to coder.
# (engine/storage/api are code, so coder is the default.)
GATE_SUBAGENT = {"static": "frontend_design", "readme": "doc_writer"}
# Robustness probe -> the behavior the fix must produce. All probes hammer
# api.py, so a robust failure always dispatches coder.
ROBUST_EXPECT = {
    "huge_duration": "POST /api/sessions with duration_s=10**15 must return 400",
    "huge_id": "GET /api/sessions/99999999999999999999 must return 400, never 500 "
               "(reject out-of-range ids before int())",
    "patch_huge_id": "PATCH /api/sessions/99999999999999999999 must return 400, never 500",
    "delete_huge_id": "DELETE /api/sessions/99999999999999999999 must return 400, never 500",
    "deep_nested": "POST with a deeply nested body must not 500 (RecursionError)",
    "after_alive": "server must still answer GET /api/sessions after hostile input",
}


def _parse_fail(line: str) -> tuple[str, str] | None:
    """Parse a verify FAIL line into (gate name, message).

    ``"FAIL engine: AssertionError: bad state"`` -> ``("engine",
    "AssertionError: bad state")``. Returns None for non-FAIL lines so a
    caller can filter a noisy stdout safely.
    """
    if not line.strip().startswith("FAIL "):
        return None
    name, _, msg = line.strip()[len("FAIL "):].partition(":")
    return name.strip(), msg.strip()


def _robust_failures(rob: dict) -> list[str]:
    """Failed robustness probes -> human-readable fix requirements."""
    out = []
    for probe, ok in (rob.get("robust") or {}).items():
        if not ok:
            out.append(f"robustness probe '{probe}': {ROBUST_EXPECT.get(probe, probe)}")
    return out


def _build_repair_brief(out: Path, fail_lines: list[str], rob: dict) -> tuple[str, list[str]]:
    """Failed gates + probes -> (repair brief, subagents to dispatch).

    Robustness failures always target api.py -> coder; each verify FAIL line
    maps through ``GATE_FILE``/``GATE_SUBAGENT``. An empty subagent list means
    nothing dispatchable (e.g. all fail lines unparseable) — the caller should
    stop repairing.
    """
    items: list[tuple[str, str]] = []
    subs: set[str] = set()
    for line in fail_lines:
        parsed = _parse_fail(line)
        if parsed is None:
            continue
        gate, msg = parsed
        f = GATE_FILE.get(gate, gate)
        subs.add(GATE_SUBAGENT.get(gate, "coder"))
        items.append((f"{out}/{f}", f"gate '{gate}' failed: {msg}"))
    for rline in _robust_failures(rob):
        subs.add("coder")
        items.append((f"{out}/api.py", rline))
    if not items:
        return "", []
    joined = "\n".join(f"- {path}: {reason}" for path, reason in items)
    brief = (
        f"A verification gate failed on the implementation in {out}. Fix the code so "
        "all gates pass.\n\nFailures:\n" + joined + "\n\n"
        f"The gate is at {out}/verify_impl.py — read it to see exactly what each gate "
        f"asserts, then after each change run `uv run python {out}/verify_impl.py` and "
        "iterate until it prints VERIFY_PASS 5/5. Keep the tests green too: "
        f"`uv run pytest -q {out}`.\n\n"
        "Rules: standard library only (no third-party imports); do not delete files; "
        "do not modify verify_impl.py; do not change the public class/function "
        "signatures — fix the implementation, not the interface.\n\n"
        "When done, report which file(s) you changed and the final VERIFY_PASS line."
    )
    return brief, sorted(subs)


# ---- run orchestration ---- #


def _make_sink(names: list[str]) -> Any:
    async def sink(run_id: str, name: str, event: object) -> None:
        if isinstance(event, SubagentRunStart):
            names.append(name)

    return sink


async def _auto_approve(tool_call: ToolCall) -> str:
    return "y"


def _build_providers(settings: Settings) -> tuple[OpenAICompatProvider, OpenAICompatProvider]:
    """Main (pro) + subagent (flash) providers, both tracking usage."""
    pro = OpenAICompatProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        track_usage=True,
    )
    flash = OpenAICompatProvider(
        model=settings.subagent_model or SUBAGENT_MODEL,
        api_key=settings.subagent_api_key,
        base_url=settings.subagent_base_url or settings.base_url,
        track_usage=True,
    )
    return pro, flash


async def _dispatch_fix(
    stack: Any, *, out: Path, brief: str, subs: list[str], advanced: bool
) -> int:
    """Dispatch one repair subagent; return the number dispatched successfully.

    advanced mode reuses the live delegate tools (``delegate_to_<name>``) so a
    fix counts against the run's subagent budget and model routing. normal mode
    spawns a fresh pro repair agent — it has no subagents, so this is the only
    repair path and the pro-token cost is exactly what the benchmark is trying
    to measure. Degrades (logs, returns 0) on any failure rather than crashing
    the benchmark.
    """
    dispatched = 0
    if advanced:
        for name in subs:
            tool = stack.agent.tools.get(f"delegate_to_{name}")
            if tool is None:
                print(f"    repair: no delegate_to_{name} tool", flush=True)
                continue
            try:
                await tool.invoke(
                    task=brief,
                    scope=str(out),
                    expected_output="Report which file(s) changed and the final VERIFY_PASS line.",
                )
                dispatched += 1
            except Exception as exc:  # noqa: BLE001 — degrade, keep the benchmark alive
                print(
                    f"    repair: delegate_to_{name} failed: {type(exc).__name__}: {exc}",
                    flush=True,
                )
    else:
        from harness.core.agent import Agent
        from harness.tools.builtin import builtin_registry

        agent = Agent(
            name="repair",
            instructions=brief,
            tools=builtin_registry(),
            model=stack.agent.model,
            max_turns=20,
        )
        try:
            await stack.runner.run(agent, brief)
            dispatched += 1
        except Exception as exc:  # noqa: BLE001 — degrade, keep the benchmark alive
            print(
                f"    repair: normal-mode fix failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
    return dispatched


async def _repair_loop(
    stack: Any, out: Path, *, label: str, i: int, advanced: bool, max_rounds: int
) -> tuple[int, list[str], int, dict, int, int]:
    """Gate-driven repair: dispatch fix subagents until gates pass or progress stalls.

    Re-runs the gate, pytest, and robustness audit after each round. Stops when
    (verify, robust) both reach max, or when a round produces no improvement
    over the previous one — a stall means the fix subagent can't see its own
    problem, so more rounds just burn tokens. Returns the final
    (verify_pass, fail_lines, pytest_passed, rob, rounds, dispatched_total).
    """
    verify_pass, fail_lines = _run_verify(out)
    pytest_passed = _run_pytest(out)
    rob = robust_score(label, i, out)
    rounds = dispatched_total = 0
    while rounds < max_rounds and not (verify_pass == 5 and rob.get("robust_pass") == 6):
        brief, subs = _build_repair_brief(out, fail_lines, rob)
        if not subs:
            break
        prev = (verify_pass, rob.get("robust_pass", 0))
        print(
            f"    repair round {rounds + 1}: {subs} (verify={verify_pass}/5 "
            f"robust={rob.get('robust_pass', 0)}/6)",
            flush=True,
        )
        dispatched_total += await _dispatch_fix(
            stack, out=out, brief=brief, subs=subs, advanced=advanced
        )
        rounds += 1
        verify_pass, fail_lines = _run_verify(out)
        pytest_passed = _run_pytest(out)
        rob = robust_score(label, i, out)
        if (verify_pass, rob.get("robust_pass", 0)) == prev:
            # No gate improvement — stop rather than spend budget on more
            # rounds that fix nothing.
            print("    repair: no gate improvement — stopping", flush=True)
            break
    return verify_pass, fail_lines, pytest_passed, rob, rounds, dispatched_total


def _metrics(
    *,
    seconds: float,
    pro_records: list[dict[str, Any]],
    flash_records: list[dict[str, Any]],
    subagent_runs: int,
    agents: list[str],
    verify_pass: int,
    pytest_passed: bool,
    robust_pass: int,
    repair_rounds: int = 0,
    repair_dispatches: int = 0,
) -> dict[str, Any]:
    """Aggregate usage records + quality gates into a run's metrics dict."""
    pro_u = sum_usage(pro_records)
    flash_u = sum_usage(flash_records)
    return {
        "seconds": seconds,
        "subagent_runs": subagent_runs,
        "agents": sorted(set(agents)),
        "pro_input_tokens": pro_u["prompt"],
        "pro_output_tokens": pro_u["completion"],
        "pro_reasoning_tokens": pro_u["reasoning"],
        "flash_input_tokens": flash_u["prompt"],
        "flash_output_tokens": flash_u["completion"],
        "pro_tokens": pro_u["total"],
        "flash_tokens": flash_u["total"],
        "cost_usd": round(cost(pro_records) + cost(flash_records), 6),
        "verify_pass": verify_pass,
        "pytest_passed": pytest_passed,
        "robust_pass": robust_pass,
        "repair_rounds": repair_rounds,
        "repair_dispatches": repair_dispatches,
    }


async def _run_once(
    settings: Settings,
    out_dir: Path,
    *,
    label: str,
    i: int,
    forced: bool,
    advanced: bool,
    partial: dict[str, Any],
) -> dict[str, Any]:
    pro, flash = _build_providers(settings)
    started = time.monotonic()
    stack = await build_core_stack(
        settings, provider=pro, subagent_provider=flash, prompt=_auto_approve
    )
    agent_names: list[str] = []
    if advanced:
        add_example_subagents(
            stack,
            advanced=True,
            subagent_model=settings.subagent_model or SUBAGENT_MODEL,
            on_event=_make_sink(agent_names),
            subagent_router=settings.subagent_router,
        )
    prompt = _prompt_forced(str(out_dir)) if forced else _prompt(str(out_dir))
    verify_pass = pytest_passed = 0
    rob: dict = {"robust_pass": 0}
    repair_rounds = repair_dispatches = 0
    try:
        # Self-contained timeout (#4): wait_for lives inside _run_once, so a
        # timeout cancels only the inner runner task. The finally below then
        # snapshots usage — a run that timed out after doing real work no
        # longer records as zero-token. (Cancellation of _run_once itself —
        # e.g. the outer fallback wait_for in _run_all — runs this finally
        # too, which is the whole point: CancelledError is a BaseException
        # that used to bypass the usage collection entirely.)
        try:
            await asyncio.wait_for(
                stack.runner.run(stack.agent, prompt), timeout=RUN_TIMEOUT
            )
        except TimeoutError:
            reason = "timeout"
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the benchmark
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = "ok"
        # Post-run gates + optional gate-driven repair (#1). Wrapped so a gate
        # crash (e.g. robustness probe failing to import api.py) degrades the
        # run instead of aborting the whole benchmark. store must stay open
        # here — a repair subagent writes files through the SnapshotExecutor.
        try:
            verify_pass, _ = _run_verify(out_dir)
            pytest_passed = _run_pytest(out_dir)
            rob = robust_score(label, i, out_dir)
            if REPAIR_MAX > 0:
                (verify_pass, _fail_lines, pytest_passed, rob,
                 repair_rounds, repair_dispatches) = await _repair_loop(
                    stack, out_dir, label=label, i=i,
                    advanced=advanced, max_rounds=REPAIR_MAX,
                )
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the benchmark
            reason = f"{type(exc).__name__}: {exc} (post-run gates/repair)"
    finally:
        # Each run builds its own Store (SQLite over aiosqlite). Closing it here
        # — even on exception or cancellation — lets the non-daemon worker thread
        # exit, so the process doesn't linger after main() returns. Usage is
        # snapshotted into `partial` so a salvaged run keeps its tokens. Close
        # lives in the OUTERMOST finally so a repair loop's write_file snapshots
        # still land before the store goes away.
        await stack.store.close()
        partial["pro_usage"] = list(pro.usage_log)
        partial["flash_usage"] = list(flash.usage_log)
    seconds = time.monotonic() - started
    return {
        "mode": label,
        "run": i,
        "out": str(out_dir),
        "reason": reason,
        "metrics": _metrics(
            seconds=seconds,
            pro_records=partial["pro_usage"],
            flash_records=partial["flash_usage"],
            subagent_runs=len(agent_names),
            agents=agent_names,
            verify_pass=verify_pass,
            pytest_passed=pytest_passed,
            robust_pass=rob.get("robust_pass", 0),
            repair_rounds=repair_rounds,
            repair_dispatches=repair_dispatches,
        ),
    }


def _salvage(
    label: str,
    i: int,
    out_dir: Path,
    *,
    reason: str,
    seconds: float,
    partial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a run that ended without clean metrics (timeout / API failure).

    ``partial`` carries the usage snapshots taken in ``_run_once``'s finally;
    non-empty records are merged in instead of hardcoding zero (#4).
    """
    verify_pass, _ = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    rob = robust_score(label, i, out_dir)
    p = partial or {}
    return {
        "mode": label,
        "run": i,
        "out": str(out_dir),
        "reason": reason,
        "metrics": _metrics(
            seconds=seconds,
            pro_records=p.get("pro_usage", []),
            flash_records=p.get("flash_usage", []),
            subagent_runs=0,
            agents=[],
            verify_pass=verify_pass,
            pytest_passed=pytest_passed,
            robust_pass=rob.get("robust_pass", 0),
        ),
    }


def _load_results() -> tuple[list[dict[str, Any]], set[tuple[str, int]]]:
    """Read existing results JSONL into (records, done keys); empty if none."""
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
    return runs, done


def _append_record(record: dict[str, Any]) -> None:
    with RESULTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


async def _run_all(settings: Settings, tmp: Path) -> int:
    """Per-run loop; returns the process exit code."""
    runs, done = _load_results()
    if done:
        print(
            f"resume: {len(done)} runs already recorded in {RESULTS_FILE} — skipping them",
            flush=True,
        )
    for label in GROUPS:
        advanced = label == "forced-advanced"
        forced = advanced  # both forced-* groups use the forced prompt
        n_runs = RUNS_SPEC.get(label, DEFAULT_RUNS)  # #7: unequal per-group samples
        for i in range(1, n_runs + 1):
            if (label, i) in done:
                continue
            out_dir = tmp / f"{label}-{i}"
            out_dir.mkdir(parents=True, exist_ok=True)
            partial: dict[str, Any] = {}
            try:
                record = await asyncio.wait_for(
                    _run_once(
                        settings, out_dir, label=label, i=i, forced=forced,
                        advanced=advanced, partial=partial,
                    ),
                    timeout=RUN_TIMEOUT + 120,  # outer fallback; inner timeout does the work
                )
            except TimeoutError:
                print(
                    f"  {label}-{i}: TIMEOUT after {RUN_TIMEOUT + 120:.0f}s — salvaging",
                    flush=True,
                )
                record = _salvage(
                    label, i, out_dir,
                    reason="timeout", seconds=RUN_TIMEOUT + 120, partial=partial,
                )
            m = record["metrics"]
            print(
                f"  ran {label}-{i}: {record['reason']} pro_tok={m['pro_tokens']} "
                f"flash_tok={m['flash_tokens']} sub={m['subagent_runs']} "
                f"verify={m['verify_pass']}/5 pytest={m['pytest_passed']} "
                f"robust={m['robust_pass']}/6 repair={m['repair_rounds']} "
                f"cost=${m['cost_usd']} wall={m['seconds']:.1f}s",
                flush=True,
            )
            runs.append(record)
            _append_record(record)

    if not runs:
        print("  no runs completed — aborting", file=sys.stderr)
        return 1

    by = {g: [r for r in runs if r["mode"] == g] for g in GROUPS}
    print("\n== token-economy comparison ==")
    for g in GROUPS:
        rs = by[g]
        if not rs:
            print(f"  {g:15s} n=0 (no completed runs)")
            continue
        ms = [r["metrics"] for r in rs]
        print(f"  {g:15s} n={len(rs)}")
        for key, unit in (
            ("pro_tokens", ""),
            ("pro_input_tokens", ""),
            ("flash_tokens", ""),
            ("cost_usd", " $"),
            ("seconds", " s"),
            ("subagent_runs", ""),
            ("repair_rounds", ""),
            ("verify_pass", "/5"),
            ("pytest_passed", ""),
            ("robust_pass", "/6"),
        ):
            print(f"      {key:18s} {_fmt_stats([float(m[key]) for m in ms])}{unit}")

    # Reduction compares clean runs only (reason == "ok"); a salvaged timeout
    # records pro_tokens=0, which would masquerade as a huge saving.
    def clean(g: str) -> list[float]:
        return [r["metrics"]["pro_tokens"] for r in by.get(g, []) if r["reason"] == "ok"]

    n = clean("normal")
    a = clean("forced-advanced")
    red = pro_reduction(a, n)
    if red is not None:
        print(
            f"\n  PRO-TOKEN REDUCTION (clean runs): {red * 100:.1f}%  "
            f"(normal n={len(n)} mean={sum(n) / len(n):.0f} -> "
            f"advanced n={len(a)} mean={sum(a) / len(a):.0f})"
        )
    else:
        print("\n  PRO-TOKEN REDUCTION: undefined (need >=1 completed run per group)")
    return 0


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    settings = Settings.load(REPO_ROOT / ".env")
    if not settings.api_key:
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2
    if not settings.subagent_api_key:
        print("no HARNESS_SUBAGENT_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2

    # The coordinator ships bundled (src/harness/skills/bundled/subagents/
    # coordinator.yaml); no runtime write needed.
    tmp = Path(tempfile.mkdtemp(prefix="harness-token-econ-"))
    settings = settings.replace(db_path=str(tmp / "harness.db"), subagent_budget=SUBAGENT_BUDGET)
    return asyncio.run(_run_all(settings, tmp))


if __name__ == "__main__":
    sys.exit(main())
