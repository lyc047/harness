"""ContextStore: offload files + compaction transcripts on disk."""

from __future__ import annotations

from harness.context.store import ContextStore, estimate_tokens
from harness.core.messages import Message


def test_estimate_tokens():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 400) == 100


def test_offload_writes_and_relpath(tmp_path):
    store = ContextStore(tmp_path)
    path = store.offload("sess1", "call-abc", "x" * 100)
    assert path.read_text(encoding="utf-8") == "x" * 100
    assert store.relpath(path) == "sess1/offload_call-abc.txt"


def test_write_transcript_jsonl(tmp_path):
    store = ContextStore(tmp_path)
    msgs = [Message.system("hi"), Message.user("yo")]
    path = store.write_transcript("sess1", 3, msgs)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"role": "user"' in lines[1]
    rel = store.relpath(path)
    assert rel.startswith("sess1/transcript_3_") and rel.endswith(".jsonl")


def test_cleanup_removes_session_dir(tmp_path):
    store = ContextStore(tmp_path)
    store.offload("sess1", "c1", "data")
    store.write_transcript("sess1", 0, [Message.user("x")])
    store.cleanup("sess1")
    assert not (tmp_path / "sess1").exists()
    # 其他 session 不受影响
    store.offload("sess2", "c2", "data2")
    assert (tmp_path / "sess2").exists()


def test_session_id_path_traversal_guarded(tmp_path):
    store = ContextStore(tmp_path)
    store.offload("../evil", "c1", "x")
    assert not (tmp_path.parent / "evil").exists()


def test_cleanup_rejects_unsafe_session_ids(tmp_path):
    root = tmp_path / "ctx"
    root.mkdir()
    store = ContextStore(root)
    store.offload("s1", "c1", "x")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    store.cleanup("..")
    store.cleanup(".")
    store.cleanup("")
    # 全部 no-op：根目录的父目录、root 自身、s1 目录都完好
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (root / "s1").exists()
    assert root.exists()


def test_offload_sanitizes_tool_call_id(tmp_path):
    store = ContextStore(tmp_path)
    path = store.offload("s1", "../../evil", "content")
    session_dir = (tmp_path / "s1").resolve()
    assert path.resolve().parent == session_dir
    assert path.read_text(encoding="utf-8") == "content"
    # 分隔符被清洗，文件名里不再有 / 或 \
    assert "/" not in path.name and "\\" not in path.name
    # 没有文件逃逸到 s1 之外
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "evil").exists()


def test_transcript_files_unique_per_run(tmp_path):
    store = ContextStore(tmp_path)
    p1 = store.write_transcript("s1", 0, [Message.user("one")])
    p2 = store.write_transcript("s1", 0, [Message.user("two")])
    assert p1 != p2
    assert p1.exists() and p2.exists()
    assert "one" in p1.read_text(encoding="utf-8")
    assert "two" in p2.read_text(encoding="utf-8")
