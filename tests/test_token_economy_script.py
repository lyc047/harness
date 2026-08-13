"""Pure helpers of the token-economy benchmark (no network)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e2e_token_economy import (  # noqa: E402
    GATE_FILE,
    GATE_SUBAGENT,
    ROBUST_EXPECT,
    _build_repair_brief,
    _fmt_stats,
    _parse_fail,
    _robust_failures,
    cost,
    parse_runs,
    pro_reduction,
    sum_usage,
)

REC = {
    "model": "deepseek-v4-pro",
    "prompt_tokens": 1_000_000,
    "completion_tokens": 500_000,
    "reasoning_tokens": 100_000,
}


def test_sum_usage_aggregates():
    # Second record zeroes prompt/completion but still carries reasoning.
    s = sum_usage([REC, {**REC, "prompt_tokens": 0, "completion_tokens": 0}])
    assert s["prompt"] == 1_000_000
    assert s["completion"] == 500_000
    assert s["reasoning"] == 200_000
    assert s["total"] == 1_500_000


def test_sum_usage_empty():
    assert sum_usage([]) == {"prompt": 0, "completion": 0, "reasoning": 0, "total": 0}


def test_cost_uses_per_model_pricing():
    # 1M prompt @1.68 + 0.5M completion @3.36
    assert cost([REC]) == 1.68 + 1.68


def test_cost_skips_unknown_model():
    assert cost([{**REC, "model": "no-such-model"}]) == 0.0


def test_pro_reduction_pct():
    assert pro_reduction([100], [1000, 2000]) == pytest.approx(1 - 100 / 1500)
    assert pro_reduction([], [1000]) == 1.0  # advanced burned nothing
    assert pro_reduction([100], []) is None
    assert pro_reduction([100], [0, 0]) is None  # normal base zero -> undefined


def test_parse_runs_flat_int_applies_to_all_groups():
    assert parse_runs("5") == {"normal": 5, "forced-advanced": 5}
    assert parse_runs("3") == {"normal": 3, "forced-advanced": 3}


def test_parse_runs_unequal_per_group():
    spec = "normal=3,forced-advanced=5"
    assert parse_runs(spec) == {"normal": 3, "forced-advanced": 5}


def test_parse_runs_partial_group_spec():
    # A group missing from the spec falls back to DEFAULT_RUNS at the call site.
    assert parse_runs("forced-advanced=5") == {"forced-advanced": 5}


def test_parse_runs_blank_is_empty():
    assert parse_runs("") == {}
    assert parse_runs(None) == {}


def test_parse_runs_rejects_bad_input():
    for bad in ("bogus=3", "normal=abc", "normal", "normal=2, =3", "3=3"):
        with pytest.raises(ValueError):
            parse_runs(bad)


def test_fmt_stats_mean_median_spread():
    assert _fmt_stats([10.0, 20.0, 30.0]) == "20.0 (20.0) [10.0–30.0] ±10.0"


def test_fmt_stats_single_value_no_std():
    assert _fmt_stats([7.0]) == "7.0 (7.0) [7.0–7.0] ±0.0"


def test_fmt_stats_empty():
    assert _fmt_stats([]) == "n/a"


def test_import_tolerates_grouped_runs_env():
    """Regression: importing e2e_token_economy with HARNESS_COMPARE_RUNS in the
    newer 'group=n' form must not crash the legacy v2/v6 modules it imports
    (they eagerly int() the shared env at module load)."""
    env = {**os.environ, "HARNESS_COMPARE_RUNS": "normal=3,forced-advanced=5"}
    code = (
        f"import sys; sys.path.insert(0, r'{SCRIPTS}'); "
        "import e2e_token_economy; "
        "assert e2e_token_economy.RUNS_SPEC == "
        "{'normal': 3, 'forced-advanced': 5}"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


# ---- #1 gate-driven repair helpers ---- #


def test_parse_fail_parses_gate_and_message():
    assert _parse_fail("FAIL engine: AssertionError: bad state") == (
        "engine",
        "AssertionError: bad state",
    )
    # a message with extra colons keeps everything after the gate name
    assert _parse_fail("FAIL api: ValueError: 500: bad") == (
        "api",
        "ValueError: 500: bad",
    )
    # non-FAIL lines and empty input are ignored, not fatal
    assert _parse_fail("PASS engine") is None
    assert _parse_fail("  FAIL engine: x") == ("engine", "x")  # leading ws tolerated
    assert _parse_fail("") is None


def test_gate_to_file_and_subagent_mapping():
    assert GATE_FILE == {
        "engine": "engine.py",
        "storage": "storage.py",
        "api": "api.py",
        "static": "static/",
        "readme": "README.md",
    }
    # static/readme go to their specialty subagents; everything else to coder
    assert GATE_SUBAGENT.get("static") == "frontend_design"
    assert GATE_SUBAGENT.get("readme") == "doc_writer"
    assert GATE_SUBAGENT.get("engine", "coder") == "coder"
    assert GATE_SUBAGENT.get("api", "coder") == "coder"
    assert GATE_SUBAGENT.get("storage", "coder") == "coder"


def test_robust_failures_lists_only_failed_probes():
    rf = _robust_failures({"robust": {"huge_id": False, "after_alive": True}})
    assert len(rf) == 1
    assert "huge_id" in rf[0]
    assert ROBUST_EXPECT["huge_id"] in rf[0]
    # no probes / all passing -> empty
    assert _robust_failures({}) == []
    assert _robust_failures({"robust": {}}) == []


def test_build_repair_brief_maps_gates_to_subagents(tmp_path):
    out = tmp_path / "normal-1"
    brief, subs = _build_repair_brief(
        out, ["FAIL api: ValueError: 500"], {"robust": {"huge_id": True}}
    )
    assert subs == ["coder"]
    assert f"{out}/api.py" in brief
    assert "gate 'api' failed" in brief
    assert "verify_impl.py" in brief

    brief, subs = _build_repair_brief(out, ["FAIL static: AssertionError: x"], {})
    assert subs == ["frontend_design"]
    assert f"{out}/static/" in brief

    brief, subs = _build_repair_brief(out, ["FAIL readme: AssertionError: empty"], {})
    assert subs == ["doc_writer"]
    assert f"{out}/README.md" in brief


def test_build_repair_brief_folds_robust_failures(tmp_path):
    out = tmp_path / "normal-1"
    # robust failures always target api.py -> coder
    brief, subs = _build_repair_brief(
        out, [], {"robust": {"huge_id": False, "huge_duration": False}}
    )
    assert subs == ["coder"]
    assert "robustness probe 'huge_id'" in brief
    assert "robustness probe 'huge_duration'" in brief
    assert f"{out}/api.py" in brief


def test_build_repair_brief_dedupes_subagents_and_empty(tmp_path):
    out = tmp_path / "normal-1"
    # engine + api failures both map to coder -> single dispatch, not two
    brief, subs = _build_repair_brief(
        out, ["FAIL engine: AssertionError: a", "FAIL api: AssertionError: b"], {}
    )
    assert subs == ["coder"]

    # unparseable lines only -> nothing to dispatch
    assert _build_repair_brief(out, ["not a FAIL line", "PASS engine"], {}) == ("", [])
