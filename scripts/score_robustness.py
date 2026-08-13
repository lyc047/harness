"""Adversarial-robustness audit for completed pomodoro benchmark runs.

Spawns a fresh subprocess per {out} dir (so each target's api/storage is
imported in its own interpreter — no module cache leakage), boots the app on
an ephemeral port, and fires hostile inputs at it:

  huge_duration  POST /api/sessions {"duration_s": 10**15}  -> 400 is robust
  huge_id        GET  /api/sessions/99999999999999999999    -> 400 is robust
  deep_nested    POST with 5-deep nested payload            -> any 2xx/4xx ok;
                                                              500 = crash
  patch_huge_id  PATCH /api/sessions/99999999999999999999   -> 400 is robust
  delete_huge_id DELETE /api/sessions/99999999999999999999  -> 400 is robust
  after_alive    GET  /api/sessions (after the above)       -> 200 = survived

Robust = graceful 4xx on absurd-but-parseable input; a 500 (uncaught crash)
or an accepted-forever duration (201 on 10^15 s) is fragile. The probe never
touches the model's tests or verify_impl — it is a pure black-box HTTP audit.

Reads the runs JSONL (default $TEMP/harness-pomo-results.jsonl), same shape
as score_existing_v2.py.

Env:
  HARNESS_RESULTS_FILE   override the results file path
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = Path(os.environ.get("TEMP", ".")) / "harness-pomo-results.jsonl"
RESULTS_FILE = Path(os.environ.get("HARNESS_RESULTS_FILE", str(DEFAULT_RESULTS)))

PROBE = r"""
import json, pathlib, sys, threading, urllib.request, urllib.error
out = pathlib.Path(sys.argv[1])
db = out / '_rob_db.db'
sys.path.insert(0, str(out))
import api, storage
try:
    db.unlink(missing_ok=True)
except OSError:
    pass
store = storage.SessionStore(db)
server = api.create_server(store, static_dir=out / 'static', host='127.0.0.1', port=0)
port = server.server_address[1]
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
base = f'http://127.0.0.1:{port}'

def req(method, path, body=None):
    headers = {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    r = urllib.request.Request(base + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return 'EXC:' + type(e).__name__

res = {
    'huge_duration': req('POST', '/api/sessions', json.dumps({'duration_s': 10**15}).encode()),
    'huge_id': req('GET', '/api/sessions/99999999999999999999'),
    'deep_nested': req('POST', '/api/sessions',
                       json.dumps({'duration_s': 25, 'x': {'x': {'x': {'x': {'x': 1}}}}}).encode()),
    'patch_huge_id': req('PATCH', '/api/sessions/99999999999999999999',
                         json.dumps({'note': 'x'}).encode()),
    'delete_huge_id': req('DELETE', '/api/sessions/99999999999999999999'),
    'after_alive': req('GET', '/api/sessions'),
}
server.shutdown()
t.join(timeout=5)
try:
    db.unlink()
except OSError:
    pass
print(json.dumps(res))
"""


def score(mode: str, run: int, out_dir: Path) -> dict:
    probe = Path(tempfile.gettempdir()) / f"probe_{mode}_{run}.py"
    probe.write_text(PROBE, encoding="utf-8")
    proc = subprocess.run(
        ["uv", "run", "python", str(probe), str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (proc.stdout or proc.stderr).strip()[:200]}
    robust = {
        "huge_duration": res.get("huge_duration") == 400,
        "huge_id": res.get("huge_id") == 400,
        "deep_nested": (
            isinstance(res.get("deep_nested"), int) and 200 <= res["deep_nested"] < 500
        ),
        "patch_huge_id": res.get("patch_huge_id") == 400,
        "delete_huge_id": res.get("delete_huge_id") == 400,
        "after_alive": res.get("after_alive") == 200,
    }
    return {"codes": res, "robust": robust, "robust_pass": sum(robust.values())}


def main() -> int:
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
        s = score(rec["mode"], rec["run"], out)
        if "error" in s:
            print(f"  {rec['mode']}-{rec['run']}: ERROR {s['error']}", flush=True)
            by_mode.setdefault(rec["mode"], []).append(
                {"run": rec["run"], "codes": None, "robust": None, "robust_pass": None}
            )
            continue
        print(
            f"  {rec['mode']}-{rec['run']}: {s['codes']}  robust={s['robust_pass']}/6",
            flush=True,
        )
        by_mode.setdefault(rec["mode"], []).append(
            {
                "run": rec["run"],
                "codes": s["codes"],
                "robust": s["robust"],
                "robust_pass": s["robust_pass"],
            }
        )

    print("\n== adversarial robustness summary ==")
    for mode, items in sorted(by_mode.items()):
        passes = [i["robust_pass"] for i in items if i["robust_pass"] is not None]
        codes = [i["codes"] for i in items if i["codes"]]
        if not passes:
            print(f"  {mode:15s} n={len(items)}  all errored")
            continue
        min_, max_ = min(passes), max(passes)
        # Highlight any crash (500) or absurd-accept (201 on huge duration)
        fragile = [
            f"{i['run']}:{c['huge_id']}/500={c['huge_id'] == 500}"
            for i, c in zip(items, codes, strict=False)
            if c and c.get("huge_id") == 500
        ]
        accepted = [
            i["run"]
            for i, c in zip(items, codes, strict=False)
            if c and c.get("huge_duration") == 201
        ]
        print(
            f"  {mode:15s} n={len(items)}  robust {min_}-{max_}/6  "
            f"fragile500(huge_id)={fragile or '-'}  accepts10^15={accepted or '-'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
