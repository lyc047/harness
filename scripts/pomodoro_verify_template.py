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
    _rm(db)
    _rm(cdb)
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
    assert "timer" in idx.lower(), "index.html has no timer element"
    assert "<button" in idx.lower(), "index.html has no <button> controls"
    for fn in ("startTimer", "pauseTimer", "resetTimer"):
        assert fn in js, f"app.js missing {fn}"
    assert "fetch" in js and "/api/sessions" in js, "app.js does not fetch /api/sessions"
    assert css.count("{") >= 15, f"style.css has {css.count('{')} rules"


@gate("readme")
def _gate_readme() -> None:
    text = (OUT / "README.md").read_text(encoding="utf-8")
    for sec in ("Overview", "Run", "API", "Tests"):
        assert sec in text, f"README missing section {sec!r}"
    assert any("python" in line for line in text.splitlines()), "README has no run command line"


def main() -> int:
    for fn in (_gate_engine, _gate_storage, _gate_api, _gate_static, _gate_readme):
        fn()
    print(f"VERIFY_PASS {len(PASSED)}/5", flush=True)
    return 0 if len(PASSED) == 5 else 1


if __name__ == "__main__":
    sys.exit(main())
