"""Harness-generated graded quality gate (v2) for the pomodoro-sprint benchmark.

Runs in each {out} dir after the model's run, like the v1 gate, but ALSO scores
graded quality (robustness under stress, edge-case coverage, code hygiene, test
thoroughness) on a 0-100 scale. The v1 binary contract gates (engine / storage /
api / static / readme) are retained for compliance; QUALITY_SCORE is the
discriminating axis the benchmark reports.

Output:
  PASS/FAIL per binary gate, VERIFY_PASS N/5
  +N per quality check (points), QUALITY_SCORE X/100

Exit code: 0 iff all 5 binary gates pass (compliance is still a hard gate).
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent
PASSED: list[str] = []
SCORE = 0


def gate(name: str):
    """Record a pass (or fail with reason) for the binary gate ``name``."""

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


def pts(name: str, n: int, fn):
    """Add ``n`` points to QUALITY_SCORE if ``fn()`` completes without raising."""
    global SCORE
    try:
        fn()
        SCORE += n
        print(f"  +{n} {name}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  0 {name}: {type(exc).__name__}: {exc}", flush=True)


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


# ---- security static sniffs (shared with v1) ---- #


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


# ---- binary contract gates (v1, kept for compliance) ---- #


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
    e.pause()
    frozen = e.elapsed_seconds()
    clock.advance(60)
    assert e.elapsed_seconds() == frozen, "elapsed advanced while paused"
    e.resume()
    clock.advance(60)
    assert e.elapsed_seconds() > frozen, "elapsed did not resume"
    e.reset()
    assert e.state() == "idle" and e.elapsed_seconds() == 0, "reset failed"
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
    reopened = storage.SessionStore(db)
    assert reopened.get(sid) is not None, "data lost on reopen"
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

    def request(method: str, path: str, body: bytes | None = None, ctype: str | None = None):
        headers = {}
        if body is not None:
            headers["Content-Type"] = ctype or "application/json"
        req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")
        except (ConnectionError, OSError, TimeoutError):
            return -1, b"", ""

    try:
        status, html, _ = request("GET", "/")
        assert status == 200, f"GET / -> {status}"
        assert b"pomodoro" in html.lower(), "GET / does not mention pomodoro"
        body = json.dumps({"duration_s": 1500, "note": "deep work"}).encode()
        status, payload, _ = request("POST", "/api/sessions", body)
        assert status == 201, f"POST valid -> {status}"
        sid = json.loads(payload)["id"]
        status, _, _ = request("POST", "/api/sessions", b"not-json")
        assert status == 400, f"POST malformed -> {status}"
        status, _, _ = request("POST", "/api/sessions", json.dumps({"note": "x"}).encode())
        assert status == 400, f"POST missing duration_s -> {status}"
        status, _, _ = request("POST", "/api/sessions", json.dumps({"duration_s": 0}).encode())
        assert status == 400, f"POST non-positive duration_s -> {status}"
        status, _, _ = request("GET", f"/api/sessions/{sid}")
        assert status == 200, f"GET existing id -> {status}"
        status, _, _ = request("GET", "/api/sessions/999999")
        assert status == 404, f"GET missing id -> {status}"
        status, payload, _ = request("GET", "/api/sessions")
        assert status == 200, f"GET list -> {status}"
        assert any(s["id"] == sid for s in json.loads(payload)), "list lacks created id"
        big = json.dumps({"duration_s": 25, "note": "x" * 70_000}).encode()
        status, _, _ = request("POST", "/api/sessions", big)
        assert status in (400, 413, -1), f"oversized body -> {status}"
        status, _, _ = request("GET", "/api/sessions")
        assert status == 200, "server died after oversized body"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)
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


# ---- graded quality checks (0-100, 5 x 4 pts per area) ---- #

MODS = ("engine.py", "storage.py", "api.py")


def _unused_imports(module: str) -> list[str]:
    src = (OUT / module).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: dict[str, str] = {}
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Name):
            used.add(node.id)
    return [name for name in imported if name not in used]


def _public_defs(module: str) -> list[tuple[int, str, bool]]:
    """(lineno, name, has_docstring) for module-level defs/classes not _-prefixed."""
    src = (OUT / module).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[tuple[int, str, bool]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            doc = ast.get_docstring(node) is not None
            out.append((node.lineno, node.name, doc))
    return out


def _fresh_engine() -> tuple[FakeClock, object]:
    from engine import PomodoroEngine

    clock = FakeClock(0.0)
    return clock, PomodoroEngine(work_minutes=25, break_minutes=5, clock=clock)


# -- engine area (4 x 5) -- #


def _eng_noops() -> None:
    """Non-applicable transitions are all no-ops (contract-implied).

    Only the pairs whose transitions DO apply change state:
      start  idle->work;  pause  work->paused, break->paused;  resume  paused->...
    Every other (method, state) pair must be a no-op.
    """
    noop_pairs = [
        ("start", "work"),
        ("start", "break"),
        ("start", "paused"),
        ("pause", "idle"),
        ("pause", "paused"),
        ("resume", "idle"),
        ("resume", "work"),
        ("resume", "break"),
    ]
    for method, target in noop_pairs:
        clock, e = _fresh_engine()
        if target == "work":
            e.start()
        elif target == "break":
            e.start()
            clock.advance(25 * 60)
        elif target == "paused":
            e.start()
            e.pause()
        before = e.state()
        getattr(e, method)()
        clock.advance(3)
        assert e.state() == before, f"{method} from {target}: {e.state()!r} != {before!r}"


def _eng_backward_clock() -> None:
    clock, e = _fresh_engine()
    e.start()
    clock.advance(100)
    clock._now -= 500  # simulate NTP correction backward
    assert e.elapsed_seconds() >= 0, f"negative elapsed {e.elapsed_seconds()}"


def _eng_cycles() -> None:
    clock, e = _fresh_engine()
    for _ in range(3):
        e.start()
        clock.advance(25 * 60)
        assert e.state() == "break", f"cycle: expected break, got {e.state()!r}"
        clock.advance(5 * 60)
        assert e.state() == "work", f"cycle: expected work, got {e.state()!r}"


def _eng_reset_states() -> None:
    for setup in ("paused", "break", "work"):
        clock, e = _fresh_engine()
        e.start()
        if setup == "paused":
            e.pause()
        elif setup == "break":
            clock.advance(25 * 60)
        e.reset()
        assert e.state() == "idle", f"reset from {setup} -> {e.state()!r}"
        assert e.elapsed_seconds() == 0, f"reset from {setup} left elapsed"


def _eng_concurrency() -> None:
    clock, e = _fresh_engine()
    errors: list[BaseException] = []

    def worker() -> None:
        e.reset()
        e.start()
        e.pause()
        e.resume()
        assert e.state() in {"idle", "work", "break", "paused"}
        e.elapsed_seconds()

    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            try:
                worker()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    ts = [threading.Thread(target=hammer) for _ in range(16)]
    for t in ts:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in ts:
        t.join(timeout=5)
    assert not errors, f"16-thread engine raised: {errors[:3]}"


# -- storage area (4 x 5) -- #


def _stg_unknown_field() -> None:
    import storage

    db = OUT / "_q_storage.db"
    _rm(db)
    store = storage.SessionStore(db)
    sid = store.create(duration_s=1500, started_at=0.0, note="a")
    try:
        r = store.update(sid, not_a_column=42)
        ok = r is False
    except (ValueError, KeyError, TypeError):
        ok = True  # an explicit, clean rejection is acceptable engineering
    except Exception:  # noqa: BLE001
        ok = False  # a raw sqlite/injection error is not
    assert ok, "update(unknown_field) neither returned False nor raised cleanly"
    got = store.get(sid)
    assert got is not None and got["note"] == "a", "unknown-field update corrupted the row"
    # DB is still fully usable after the rejected update
    s2 = store.create(duration_s=10, started_at=0.0, note="after")
    assert store.get(s2) is not None, "store unusable after rejected update"
    _rm(db)


def _stg_delete_missing() -> None:
    import storage

    db = OUT / "_q_storage.db"
    _rm(db)
    store = storage.SessionStore(db)
    d = store.create(duration_s=1, started_at=0.0, note="tmp")
    assert store.delete(d) is True
    assert store.get(d) is None, "get after delete should be None"
    assert store.update(999_999, note="x") is False, "update missing id not False"
    _rm(db)


def _stg_concurrency() -> None:
    import storage

    cdb = OUT / "_q_storage_conc.db"
    _rm(cdb)
    conc = storage.SessionStore(cdb)
    barrier = threading.Barrier(16)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            for j in range(200):
                s = conc.create(duration_s=10, started_at=0.0, note=f"w{j}")
                if j % 3 == 0:
                    conc.update(s, note="updated")
                if j % 7 == 0:
                    conc.delete(s)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert not errors, f"16-thread storage raised: {errors[:3]}"
    rows = conc.list()
    assert len(rows) == len({r["id"] for r in rows}), "duplicate ids in list"
    _rm(cdb)


def _stg_monotonic_ids() -> None:
    import storage

    db = OUT / "_q_storage.db"
    _rm(db)
    store = storage.SessionStore(db)
    for _ in range(50):
        store.create(duration_s=25, started_at=0.0, note="")
    ids = [r["id"] for r in store.list()]
    assert ids == sorted(ids) and len(set(ids)) == 50, f"ids not monotonic: {ids}"
    _rm(db)


# -- api area (4 x 5) -- #


def _api_server():
    import api
    import storage

    db = OUT / "_q_api.db"
    _rm(db)
    store = storage.SessionStore(db)
    server = api.create_server(store, static_dir=OUT / "static", host="127.0.0.1", port=0)
    port: int = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def request(method: str, path: str, body: bytes | None = None, ctype: str | None = None):
        headers = {}
        if body is not None:
            headers["Content-Type"] = ctype or "application/json"
        req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")
        except (ConnectionError, OSError, TimeoutError):
            return -1, b"", ""

    return server, thread, request, db


def _api_concurrent_posts() -> None:
    server, thread, request, db = _api_server()
    try:
        bodies = [json.dumps({"duration_s": 25, "note": f"c{i}"}).encode() for i in range(10)]
        results: list[int] = []
        barrier = threading.Barrier(10)
        errors: list[BaseException] = []

        def poster(body: bytes) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(request("POST", "/api/sessions", body)[0])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=poster, args=(b,)) for b in bodies]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not errors, f"concurrent POST raised: {errors[:3]}"
        assert all(s == 201 for s in results), f"concurrent POST statuses: {results}"
        sessions = json.loads(request("GET", "/api/sessions")[1])
        assert len({s["id"] for s in sessions}) >= 10, "concurrent POSTs lost ids"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)


def _api_json_ctype() -> None:
    server, thread, request, db = _api_server()
    try:
        status, _, ctype = request("GET", "/api/sessions")
        assert status == 200, f"list -> {status}"
        assert "application/json" in ctype.lower(), f"list Content-Type={ctype!r}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)


def _api_wrong_ctype() -> None:
    server, thread, request, db = _api_server()
    try:
        status = request("POST", "/api/sessions", b"duration_s=25", ctype="text/plain")[0]
        assert status == 400, f"POST text/plain -> {status}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)


def _api_bad_id() -> None:
    server, thread, request, db = _api_server()
    try:
        status = request("GET", "/api/sessions/abc")[0]
        assert status in (400, 404), f"GET /api/sessions/abc -> {status}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)


def _api_stress() -> None:
    server, thread, request, db = _api_server()
    try:
        # HEAD is optional for the contract; any non-500 response + survival counts
        hstatus = request("HEAD", "/")[0]
        assert hstatus in (200, 405, 501), f"HEAD / -> {hstatus}"
        for k in range(20):
            if k % 2 == 0:
                request("POST", "/api/sessions", json.dumps({"duration_s": 1}).encode())
            else:
                request("GET", "/api/sessions")
        assert request("GET", "/api/sessions")[0] == 200, "server died under rapid requests"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)


# -- static area (4 x 5) -- #


def _stt_unused_imports() -> None:
    total = sum(len(_unused_imports(m)) for m in MODS)
    assert total <= 6, f"{total} unused imports across modules"


def _stt_docstrings() -> None:
    missing = sum(1 for m in MODS for _l, _n, has in _public_defs(m) if not has)
    total = sum(len(_public_defs(m)) for m in MODS)
    if total:
        assert missing / total <= 0.2, f"{missing}/{total} public defs lack docstrings"


def _stt_no_markers() -> None:
    for m in MODS:
        for i, line in enumerate((OUT / m).read_text(encoding="utf-8").splitlines(), 1):
            up = line.upper()
            assert not any(tok in up for tok in ("TODO", "FIXME", "XXX")), f"{m}:{i} marker"


def _stt_no_dead_code() -> None:
    for m in MODS:
        tree = ast.parse((OUT / m).read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
        dead = [n for n in defined if n not in used]
        assert len(dead) <= 2, f"{m} dead defs: {dead}"


def _stt_fn_length() -> None:
    for m in MODS:
        tree = ast.parse((OUT / m).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno
                assert length <= 60, f"{m} {node.name} is {length} lines"


# -- tests area (4 x 5) -- #

TEST_NAMES = ("test_engine.py", "test_storage.py", "test_api.py")


def _tst_files_exist() -> None:
    missing = [p for p in TEST_NAMES if not (OUT / p).is_file()]
    assert not missing, f"test files missing: {missing}"


def _tst_fn_count() -> None:
    count = 0
    for p in TEST_NAMES:
        if not (OUT / p).is_file():
            continue
        tree = ast.parse((OUT / p).read_text(encoding="utf-8"))
        count += sum(
            1
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        )
    assert count >= 12, f"only {count} test functions"


def _tst_assert_count() -> None:
    total = sum(
        (OUT / p).read_text(encoding="utf-8").count("assert")
        for p in TEST_NAMES
        if (OUT / p).is_file()
    )
    assert total >= 40, f"only {total} asserts"


def _tst_engine_vocab() -> None:
    eng_src = (OUT / "test_engine.py").read_text(encoding="utf-8").lower()
    for word in ("work", "break", "pause", "reset"):
        assert word in eng_src, f"test_engine.py never exercises {word!r}"


def _tst_http_codes() -> None:
    api_src = (OUT / "test_api.py").read_text(encoding="utf-8")
    for code in ("400", "404"):
        assert code in api_src, f"test_api.py never asserts {code}"


def _run_quality_checks() -> None:
    for name, n, fn in (
        # engine (4 x 5)
        ("engine: non-applicable transitions are all no-ops", 4, _eng_noops),
        ("engine: backward clock never yields negative elapsed", 4, _eng_backward_clock),
        ("engine: 3 full work->break->work cycles", 4, _eng_cycles),
        ("engine: reset from work/pause/break -> idle/0", 4, _eng_reset_states),
        ("engine: 16-thread hammering stays consistent", 4, _eng_concurrency),
        # storage (4 x 5)
        ("storage: unknown field update -> False, row intact", 4, _stg_unknown_field),
        ("storage: delete -> get None; update missing -> False", 4, _stg_delete_missing),
        ("storage: 16x200 mixed create/update/delete, no dup ids", 4, _stg_concurrency),
        ("storage: list() ids monotonic", 4, _stg_monotonic_ids),
        # api (4 x 5)
        ("api: 10 concurrent POSTs all 201, distinct ids", 4, _api_concurrent_posts),
        ("api: JSON content-type on list", 4, _api_json_ctype),
        ("api: POST text/plain -> 400", 4, _api_wrong_ctype),
        ("api: GET non-numeric id -> 400/404", 4, _api_bad_id),
        ("api: HEAD survival + 20 rapid requests", 4, _api_stress),
        # static (4 x 5)
        ("static: <=6 unused imports across modules", 4, _stt_unused_imports),
        ("static: >=80% public defs documented", 4, _stt_docstrings),
        ("static: no TODO/FIXME/XXX markers", 4, _stt_no_markers),
        ("static: <=2 dead defs per module", 4, _stt_no_dead_code),
        ("static: no function over 60 lines", 4, _stt_fn_length),
        # tests (4 x 5)
        ("tests: all 3 test files present", 4, _tst_files_exist),
        ("tests: >=12 test functions", 4, _tst_fn_count),
        ("tests: >=40 asserts", 4, _tst_assert_count),
        ("tests: engine tests exercise work/break/pause/reset", 4, _tst_engine_vocab),
        ("tests: api tests assert 400/404", 4, _tst_http_codes),
    ):
        pts(name, n, fn)


def main() -> int:
    for fn in (_gate_engine, _gate_storage, _gate_api, _gate_static, _gate_readme):
        fn()
    _run_quality_checks()
    print(f"VERIFY_PASS {len(PASSED)}/5", flush=True)
    print(f"QUALITY_SCORE {SCORE}/100", flush=True)
    return 0 if len(PASSED) == 5 else 1


if __name__ == "__main__":
    sys.exit(main())
