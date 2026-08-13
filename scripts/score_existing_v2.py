"""Re-score already-completed benchmark runs with the v2 graded gate.

Reads the runs JSONL (default $TEMP/harness-pomo-results.jsonl), copies
pomodoro_verify_template_v2.py into each recorded {out} dir as verify2_impl.py,
runs it, and prints VERIFY_PASS + QUALITY_SCORE per run grouped by mode. This
lets the benchmark be retroactively re-scored with a stricter gate without
re-running the model.

Env:
  HARNESS_RESULTS_FILE   override the results file path
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = Path(os.environ.get("TEMP", ".")) / "harness-pomo-results.jsonl"
RESULTS_FILE = Path(os.environ.get("HARNESS_RESULTS_FILE", str(DEFAULT_RESULTS)))
GATE = REPO_ROOT / "scripts" / "pomodoro_verify_template_v2.py"


def main() -> int:
    if not GATE.is_file():
        print(f"missing {GATE}", file=sys.stderr)
        return 1
    if not RESULTS_FILE.is_file():
        print(f"no results file at {RESULTS_FILE}", file=sys.stderr)
        return 1
    records = [
        json.loads(line)
        for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_mode: dict[str, list[dict]] = {}
    for rec in records:
        out = Path(rec["out"])
        if not out.is_dir():
            print(f"SKIP {rec['mode']}-{rec['run']}: {out} missing")
            continue
        dest = out / "verify2_impl.py"
        dest.write_text(GATE.read_text(encoding="utf-8"), encoding="utf-8")
        proc = subprocess.run(
            ["uv", "run", "python", str(dest)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        q = re.search(r"QUALITY_SCORE (\d+)/100", proc.stdout)
        v = re.search(r"VERIFY_PASS (\d+)/5", proc.stdout)
        score = int(q.group(1)) if q else None
        verify = int(v.group(1)) if v else None
        by_mode.setdefault(rec["mode"], []).append(
            {"run": rec["run"], "verify": verify, "score": score}
        )
        print(
            f"  {rec['mode']}-{rec['run']}: verify={verify}/5 quality={score}/100",
            flush=True,
        )
    print("\n== v2 graded summary ==")
    for mode, items in sorted(by_mode.items()):
        scores = [i["score"] for i in items if i["score"] is not None]
        verifies = [i["verify"] for i in items if i["verify"] is not None]
        med = sorted(scores)[len(scores) // 2] if scores else None
        print(
            f"  {mode:15s} n={len(items)}  verify "
            f"{min(verifies)}-{max(verifies)}/5  quality "
            f"{min(scores)}-{max(scores)}/100 (median {med})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
