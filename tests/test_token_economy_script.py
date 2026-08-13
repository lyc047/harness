"""Pure helpers of the token-economy benchmark (no network)."""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e2e_token_economy import (  # noqa: E402
    _fmt_stats,
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
