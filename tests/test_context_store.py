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
    assert store.relpath(path) == "sess1/transcript_3.jsonl"


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
