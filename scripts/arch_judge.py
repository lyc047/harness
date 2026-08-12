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
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent

DIMENSIONS = [
    "Requirements understanding",
    "Architectural soundness",
    "Trade-off awareness",
    "Completeness",
    "Deployability",
    "Risk & evolution",
]
# Reports run 15-22 KB; 8k truncated them mid-subsystem and hid the last three
# mandated sections (Key tech choices / Risks / Evolution) from the judge, which
# systematically depressed Completeness and Risk scores. 30k covers every report
# whole. Raises per-call cost (~2.5x input) but keeps judging fair.
MAX_JUDGE_CHARS = 30000

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
    scores: dict[int, int] = {}
    total: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _DIM_LINE.match(stripped)
        if m:
            scores[len(scores)] = int(m.group(1))
        m2 = _TOTAL_LINE.match(stripped)
        if m2:
            total = int(m2.group(1))
    if len(scores) != len(DIMENSIONS) or total is None:
        return None
    return {
        "scores": dict(
            zip(DIMENSIONS, [scores[i] for i in range(len(DIMENSIONS))], strict=True)
        ),
        "total": total,
    }


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
    # deepseek-v4-flash is a reasoning model: it spends a large share of its
    # output budget on reasoning_content before emitting the score lines. The
    # cap must cover reasoning + content or content comes back empty (length).
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": rendered},
        ],
        temperature=0.4,
        max_tokens=8000,
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
