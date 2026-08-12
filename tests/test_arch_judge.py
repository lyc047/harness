"""Unit tests for the blind LLM judge (scripts/arch_judge.py)."""

from __future__ import annotations

from arch_judge import DIMENSIONS, _blind_render, _parse_judge_output


def test_parse_judge_output_valid() -> None:
    text = (
        "1. Requirements understanding: 8/10 — solid constraints\n"
        "2. Architectural soundness: 7/10 — coherent\n"
        "3. Trade-off awareness: 9/10 — good trade-offs\n"
        "4. Completeness: 8/10 — complete\n"
        "5. Deployability: 6/10 — vague\n"
        "6. Risk & evolution: 7/10 — risks covered\n"
        "TOTAL: 45/60\n"
    )
    out = _parse_judge_output(text)
    assert out is not None
    assert out["total"] == 45
    assert [out["scores"][d] for d in DIMENSIONS] == [8, 7, 9, 8, 6, 7]


def test_parse_judge_output_missing_dimension() -> None:
    text = "1. Requirements understanding: 8/10 — ok\n3. Completeness: 9/10 — ok\nTOTAL: 17/60\n"
    assert _parse_judge_output(text) is None


def test_parse_judge_output_garbage() -> None:
    assert _parse_judge_output("not a judge output at all") is None


def test_blind_render_truncates() -> None:
    text = "x" * 20_000
    assert len(_blind_render(text, max_chars=8000)) == 8000


def test_blind_render_short_untouched() -> None:
    text = "short report"
    assert _blind_render(text, max_chars=8000) == "short report"
