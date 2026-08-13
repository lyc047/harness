"""Harness-generated hard gate for the pomodoro-sprint benchmark.

NOT written or run by the model. The harness copies this file into each
{out} directory after the model's run and executes it with
`uv run python {out}/verify_impl.py`. Prints PASS/FAIL per gate and a final
`VERIFY_PASS N/5` line. The 5 gates (engine / storage / api / static / readme)
encode the fixed module contracts from the task prompt; the security static
sniffs are folded into the storage and api gates.

Exit code: 0 iff all 5 gates pass.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent
PASSED: list[str] = []


def gate(name: str):
    """Record a pass (or fail with reason) for the gate named ``name``."""

    def deco(fn):
        def wrapper():
            try:
                fn()
                PASSED.append(name)
                print(f"PASS {name}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)

        return wrapper

    return deco


class FakeClock:
    """Deterministic clock for the engine's injectable ``clock`` parameter."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _rm(path: Path) -> None:
    """Unlink a scratch db; tolerate Windows file locks in temp dirs."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---- security static sniffs (folded into the storage / api gates) ---- #


def _assert_no_eval_exec() -> None:
    for p in ("engine.py", "storage.py", "api.py"):
        src = (OUT / p).read_text(encoding="utf-8")
        assert "eval(" not in src and "exec(" not in src, f"{p} uses eval()/exec()"


def _assert_no_hardcoded_secrets() -> None:
    bad = ("password = ", "secret = ", "api_key = ", "apiKey = ")
    for p in ("engine.py", "storage.py", "api.py"):
        for line in (OUT / p).read_text(encoding="utf-8").splitlines():
            stripped = line.strip().lower()
            if any(stripped.startswith(b) for b in bad):
                raise AssertionError(f"{p} hardcodes a secret: {line.strip()!r}")


def _assert_parameterized_sql() -> None:
    src = (OUT / "storage.py").read_text(encoding="utf-8")
    assert "?" in src, "storage.py uses no SQL placeholders"
    for line in src.splitlines():
        if "execute(" in line:
            assert "f\"" not in line and "f'" not in line, f"f-string SQL: {line.strip()!r}"
            if "+" in line and '"' in line:
                raise AssertionError(f"concatenated SQL: {line.strip()!r}")


@gate("engine")
def _gate_engine() -> None:
    from engine import PomodoroEngine

    clock = FakeClock(0.0)
    e = PomodoroEngine(work_minutes=25, break_minutes=5, clock=clock)
    assert e.state() == "idle", f"initial state={e.state()!r}"
    e.start()
    assert e.state() == "work", f"after start state={e.state()!r}"
    clock.advance(25 * 60)
    assert e.state() == "break", f"after work period state={e.state()!r}"
    clock.advance(5 * 60)
    assert e.state() == "work", f"after break period state={e.state()!r}"
    # pause freezes elapsed; resume continues
    e.pause()
    frozen = e.elapsed_seconds()
    clock.advance(60)
    assert e.elapsed_seconds() == frozen, "elapsed advanced while paused"
    e.resume()
    clock.advance(60)
    assert e.elapsed_seconds() > frozen, "elapsed did not resume"
    e.reset()
    assert e.state() == "idle" and e.elapsed_seconds() == 0, "reset failed"
    # concurrency: 8 threads hammering all methods must not raise / corrupt
    errors: list[BaseException] = []
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                e.reset()
                e.start()
                e.pause()
                e.resume()
                e.state()
                e.elapsed_seconds()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"engine concurrency raised: {errors[:3]}"


@gate("storage")
def _gate_storage() -> None:
    import storage

    db = OUT / "_verify_storage.db"
    _rm(db)
    store = storage.SessionStore(db)
    sid = store.create(duration_s=1500, started_at=1000.0, note="deep work")
    assert isinstance(sid, int) and sid > 0, f"create returned {sid!r}"
    got = store.get(sid)
    assert got is not None and got["duration_s"] == 1500, f"get={got!r}"
    assert got["started_at"] == 1000.0 and got["note"] == "deep work"
    assert store.get(999_999) is None, "get missing id should be None"
    assert store.delete(999_999) is False, "delete missing id should be False"
    assert store.update(999_999, note="x") is False, "update missing id should be False"
    assert any(s["id"] == sid for s in store.list()), "list lacks created id"
    # persistence across close-and-reopen
    reopened = storage.SessionStore(db)
    assert reopened.get(sid) is not None, "data lost on reopen"
    # concurrency on its OWN db for an exact count: 8 threads x 100 = 800
    cdb = OUT / "_verify_storage_conc.db"
    _rm(cdb)
    conc = storage.SessionStore(cdb)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(100):
                conc.create(duration_s=10, started_at=0.0, note="")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"storage concurrency raised: {errors[:3]}"
    assert len(conc.list()) == 800, f"expected 800 rows, got {len(conc.list())}"
    _rm(cdb)
    # Behavioral injection test: a note containing SQL must round-trip as a
    # literal. This is the real proof of `?` binding — the substring sniff
    # below can be padded with a decorative placeholder.
    injection = "x'; DROP TABLE sessions; --"
    inj_id = store.create(duration_s=25, started_at=0.0, note=injection)
    inj_got = store.get(inj_id)
    assert inj_got is not None and inj_got["note"] == injection, (
        f"injection note mutated by interpolation: {inj_got!r}"
    )
    assert any(s["id"] == sid for s in store.list()), "rows lost to injection"
    _rm(db)
    # security sniff folded into the storage gate
    _assert_parameterized_sql()
    _assert_no_hardcoded_secrets()


@gate("api")
def _gate_api() -> None:
    import api
    import storage

    db = OUT / "_verify_api.db"
    _rm(db)
    store = storage.SessionStore(db)
    server = api.create_server(store, static_dir=OUT / "static", host="127.0.0.1", port=0)
    port: int = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def request(method: str, path: str, body: bytes | None = None):
        req = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (ConnectionError, OSError, TimeoutError):
            # server closed the connection early (e.g. rejected oversized body
            # without draining); survival is re-verified by the next request
            return -1, b""

    try:
        status, html = request("GET", "/")
        assert status == 200, f"GET / -> {status}"
        assert b"pomodoro" in html.lower(), "GET / does not mention pomodoro"
        body = json.dumps({"duration_s": 1500, "note": "deep work"}).encode()
        status, payload = request("POST", "/api/sessions", body)
        assert status == 201, f"POST valid -> {status}"
        sid = json.loads(payload)["id"]
        status, _ = request("POST", "/api/sessions", b"not-json")
        assert status == 400, f"POST malformed -> {status}"
        status, _ = request("POST", "/api/sessions", json.dumps({"note": "x"}).encode())
        assert status == 400, f"POST missing duration_s -> {status}"
        status, _ = request("POST", "/api/sessions", json.dumps({"duration_s": 0}).encode())
        assert status == 400, f"POST non-positive duration_s -> {status}"
        status, _ = request("GET", f"/api/sessions/{sid}")
        assert status == 200, f"GET existing id -> {status}"
        status, _ = request("GET", "/api/sessions/999999")
        assert status == 404, f"GET missing id -> {status}"
        status, payload = request("GET", "/api/sessions")
        assert status == 200, f"GET list -> {status}"
        assert any(s["id"] == sid for s in json.loads(payload)), "list lacks created id"
        # PATCH: valid update, 404 missing, 400 malformed
        body = json.dumps({"note": "renamed"}).encode()
        status, payload = request("PATCH", f"/api/sessions/{sid}", body)
        assert status == 200, f"PATCH valid -> {status}"
        assert json.loads(payload).get("note") == "renamed", f"PATCH body={payload!r}"
        status, _ = request("PATCH", "/api/sessions/999999", body)
        assert status == 404, f"PATCH missing id -> {status}"
        status, _ = request("PATCH", f"/api/sessions/{sid}", b"not-json")
        assert status == 400, f"PATCH malformed -> {status}"
        # DELETE: existing removed, missing -> 404
        status, _ = request("DELETE", f"/api/sessions/{sid}")
        assert status in (204, 200), f"DELETE valid -> {status}"
        status, _ = request("GET", f"/api/sessions/{sid}")
        assert status == 404, f"GET after DELETE -> {status}"
        status, _ = request("DELETE", f"/api/sessions/{sid}")
        assert status == 404, f"DELETE missing -> {status}"
        # stats: consistent with the store (2 fresh rows -> total_sessions 2)
        for _ in range(2):
            status, _ = request(
                "POST", "/api/sessions", json.dumps({"duration_s": 1500, "note": "s"}).encode()
            )
            assert status == 201, f"POST for stats -> {status}"
        status, payload = request("GET", "/api/stats")
        assert status == 200, f"GET /api/stats -> {status}"
        st = json.loads(payload)
        assert st.get("total_sessions") == 2, f"stats total_sessions={st!r}"
        assert st.get("avg_duration_s", -1) >= 0, f"stats avg negative: {st!r}"
        assert st.get("total_focus_seconds", 0) >= 3000, f"stats focus seconds: {st!r}"
        # oversized body: 70 KB > 64 KB limit; server must survive
        big = json.dumps({"duration_s": 25, "note": "x" * 70_000}).encode()
        status, _ = request("POST", "/api/sessions", big)
        assert status in (400, 413, -1), f"oversized body -> {status}"
        status, _ = request("GET", "/api/sessions")
        assert status == 200, "server died after oversized body"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)
    # security sniff folded into the api gate
    _assert_no_eval_exec()
    _assert_no_hardcoded_secrets()


@gate("static")
def _gate_static() -> None:
    idx = (OUT / "static" / "index.html").read_text(encoding="utf-8")
    js = (OUT / "static" / "app.js").read_text(encoding="utf-8")
    css = (OUT / "static" / "style.css").read_text(encoding="utf-8")
    assert "style.css" in idx and "app.js" in idx, "index.html lacks style.css/app.js refs"
    low_idx = idx.lower()
    # a real timer element: an id/class containing "timer" — the old bare-word
    # substring check ("timer" in idx) passed on prose alone.
    assert re.search(r'id="[^"]*timer[^"]*"|class="[^"]*timer[^"]*"', low_idx), (
        "index.html has no element with a timer id/class"
    )
    assert re.search(r"<button", low_idx), "index.html has no <button> controls"
    for fn in ("startTimer", "pauseTimer", "resetTimer"):
        assert fn in js, f"app.js missing {fn}"
    low_js = js.lower()
    # buttons must be WIRED, not merely defined — a stub can define the three
    # functions and never bind them; that used to pass.
    assert "addeventlistener" in low_js or "onclick" in low_js, (
        "app.js wires no button clicks (no addEventListener / onclick)"
    )
    assert re.search(r"['\"]/api/sessions['\"]", js), "app.js does not fetch /api/sessions"
    assert re.search(r"['\"]/api/stats['\"]", js), "app.js does not fetch /api/stats"
    assert css.count("{") >= 15, f"style.css has {css.count('{')} rules"


@gate("readme")
def _gate_readme() -> None:
    lines = (OUT / "README.md").read_text(encoding="utf-8").splitlines()
    headers = [i for i, ln in enumerate(lines) if re.match(r"^#+\s+", ln)]
    # every section must contain at least one non-whitespace content line —
    # an empty "## Overview" used to pass the bare substring check. The first
    # header is the document title (commonly followed straight by a section
    # heading), so its "body" is not a section and is skipped.
    for idx, pos in enumerate(headers):
        if idx == 0:
            continue
        end = headers[idx + 1] if idx + 1 < len(headers) else len(lines)
        body = "".join(lines[pos + 1 : end]).strip()
        assert body, f"README section {lines[pos].strip()!r} is empty"
    header_text = [re.sub(r"^#+\s+", "", lines[i]).strip().lower() for i in headers]
    for sec in ("overview", "run", "api", "tests", "known limitations"):
        assert any(re.search(rf"\b{re.escape(sec)}\b", h) for h in header_text), (
            f"README missing section {sec!r}"
        )
    assert any("python" in ln.lower() or "uv" in ln.lower() for ln in lines), (
        "README has no run command line"
    )


def main() -> int:
    for fn in (_gate_engine, _gate_storage, _gate_api, _gate_static, _gate_readme):
        fn()
    print(f"VERIFY_PASS {len(PASSED)}/5", flush=True)
    return 0 if len(PASSED) == 5 else 1


if __name__ == "__main__":
    sys.exit(main())
