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
  HARNESS_COMPARE_RUNS      runs per group (default 3)
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

RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))
RUN_TIMEOUT = float(os.environ.get("HARNESS_COMPARE_TIMEOUT", "1800"))
GROUPS_ALL = ("normal", "forced-advanced")
_filter = [s for s in os.environ.get("HARNESS_COMPARE_GROUPS", "").split(",") if s]
GROUPS = [g for g in GROUPS_ALL if not _filter or g in _filter]
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


def _spread(vals: list[float]) -> str:
    """mean (min–max) string; 'n/a' when empty."""
    if not vals:
        return "n/a"
    m = sum(vals) / len(vals)
    return f"{m:.1f} ({min(vals):.1f}–{max(vals):.1f})"


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


async def _run_once(
    settings: Settings,
    out_dir: Path,
    *,
    label: str,
    i: int,
    forced: bool,
    advanced: bool,
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
        try:
            await stack.runner.run(stack.agent, prompt)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the benchmark
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = "ok"
    finally:
        # Each run builds its own Store (SQLite over aiosqlite). Closing it here
        # — even on exception or asyncio.wait_for cancellation above — lets the
        # non-daemon worker thread exit, so the process doesn't linger after
        # main() returns.
        await stack.store.close()
    seconds = time.monotonic() - started
    pro_u = sum_usage(pro.usage_log)
    flash_u = sum_usage(flash.usage_log)
    verify_pass = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    rob = robust_score(label, i, out_dir)
    return {
        "mode": label,
        "run": i,
        "out": str(out_dir),
        "reason": reason,
        "metrics": {
            "seconds": seconds,
            "subagent_runs": len(agent_names),
            "agents": sorted(set(agent_names)),
            "pro_input_tokens": pro_u["prompt"],
            "pro_output_tokens": pro_u["completion"],
            "pro_reasoning_tokens": pro_u["reasoning"],
            "flash_input_tokens": flash_u["prompt"],
            "flash_output_tokens": flash_u["completion"],
            "pro_tokens": pro_u["total"],
            "flash_tokens": flash_u["total"],
            "cost_usd": round(cost(pro.usage_log) + cost(flash.usage_log), 6),
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
            "robust_pass": rob.get("robust_pass", 0),
        },
    }


def _salvage(label: str, i: int, out_dir: Path, *, reason: str, seconds: float) -> dict[str, Any]:
    """Record a run that ended without clean metrics (timeout / API failure)."""
    verify_pass = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    return {
        "mode": label,
        "run": i,
        "out": str(out_dir),
        "reason": reason,
        "metrics": {
            "seconds": seconds,
            "subagent_runs": 0,
            "agents": [],
            "pro_input_tokens": 0,
            "pro_output_tokens": 0,
            "pro_reasoning_tokens": 0,
            "flash_input_tokens": 0,
            "flash_output_tokens": 0,
            "pro_tokens": 0,
            "flash_tokens": 0,
            "cost_usd": 0.0,
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
            "robust_pass": 0,
        },
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
        for i in range(1, RUNS + 1):
            if (label, i) in done:
                continue
            out_dir = tmp / f"{label}-{i}"
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                record = await asyncio.wait_for(
                    _run_once(
                        settings, out_dir, label=label, i=i, forced=forced, advanced=advanced
                    ),
                    timeout=RUN_TIMEOUT,
                )
            except TimeoutError:
                print(f"  {label}-{i}: TIMEOUT after {RUN_TIMEOUT:.0f}s — salvaging", flush=True)
                record = _salvage(label, i, out_dir, reason="timeout", seconds=RUN_TIMEOUT)
            m = record["metrics"]
            print(
                f"  ran {label}-{i}: {record['reason']} pro_tok={m['pro_tokens']} "
                f"flash_tok={m['flash_tokens']} sub={m['subagent_runs']} "
                f"verify={m['verify_pass']}/5 pytest={m['pytest_passed']} "
                f"robust={m['robust_pass']}/4 cost=${m['cost_usd']} wall={m['seconds']:.1f}s",
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
            ("robust_pass", "/4"),
        ):
            print(f"      {key:18s} {_spread([float(m[key]) for m in ms])}{unit}")

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
