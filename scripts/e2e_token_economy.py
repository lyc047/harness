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
        )
    prompt = _prompt_forced(str(out_dir)) if forced else _prompt(str(out_dir))
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
    finally:
        # Each run builds its own Store (SQLite over aiosqlite). Closing it here
        # — even on exception or cancellation — lets the non-daemon worker thread
        # exit, so the process doesn't linger after main() returns. Usage is
        # snapshotted into `partial` so a salvaged run keeps its tokens.
        await stack.store.close()
        partial["pro_usage"] = list(pro.usage_log)
        partial["flash_usage"] = list(flash.usage_log)
    seconds = time.monotonic() - started
    verify_pass = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    rob = robust_score(label, i, out_dir)
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
    verify_pass = _run_verify(out_dir)
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
                f"robust={m['robust_pass']}/6 cost=${m['cost_usd']} wall={m['seconds']:.1f}s",
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
