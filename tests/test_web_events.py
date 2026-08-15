"""Unit tests for web event serialization (pure functions, no I/O)."""

from __future__ import annotations

from harness.core.messages import Message, ToolCall
from harness.core.run_result import RunResult
from harness.core.runner import RunDone, ToolResultEvent
from harness.llm.base import LLMResponse, StreamEnd, StreamReasoning, StreamText, StreamToolCall
from harness.planning.executor import PlanDone, PlanRevised, StepEnd, StepStart
from harness.planning.models import DONE, IN_PROGRESS, PENDING, Plan, PlanStep
from harness.tools.base import ToolResult
from harness.web.events import (
    message_to_dict,
    plan_to_dict,
    serialize_event,
    serialize_messages,
    step_to_dict,
    tool_call_to_dict,
    tool_result_to_dict,
)


def test_serialize_stream_text() -> None:
    assert serialize_event(StreamText(text="hi")) == {"type": "text", "text": "hi"}


def test_serialize_reasoning() -> None:
    assert serialize_event(StreamReasoning(text="think")) == {
        "type": "reasoning",
        "text": "think",
    }


def test_serialize_tool_call() -> None:
    tc = ToolCall(id="t1", name="read_file", arguments='{"path": "a.py"}')
    frame = serialize_event(StreamToolCall(tool_call=tc))
    assert frame == {
        "type": "tool_call",
        "tool_call": {"id": "t1", "name": "read_file", "arguments": '{"path": "a.py"}'},
    }


def test_serialize_tool_call_none_is_skipped() -> None:
    assert serialize_event(StreamToolCall(tool_call=None)) is None


def test_serialize_run_done() -> None:
    result = RunResult(final_output="ok", turns=3, session_id="s1")
    frame = serialize_event(RunDone(result=result))
    assert frame == {
        "type": "run_done",
        "result": {"final_output": "ok", "turns": 3, "session_id": "s1"},
    }


def test_serialize_tool_result() -> None:
    tc = ToolCall(id="t1", name="bash", arguments="{}")
    frame = serialize_event(ToolResultEvent(tc, ToolResult.ok("$ ls\nok")))
    assert frame == {
        "type": "tool_result",
        "tool_call_id": "t1",
        "name": "bash",
        "content": "$ ls\nok",
        "is_error": False,
        "truncated": False,
        "offloaded": "",
    }


def test_serialize_tool_result_with_truncation() -> None:
    big = "x" * 200_000  # over the default 100_000 cap
    tc = ToolCall(id="t1", name="bash", arguments="{}")
    frame = serialize_event(ToolResultEvent(tc, ToolResult.ok(big)))
    assert frame["truncated"] is True
    assert len(frame["content"]) < 100_100
    assert "truncated" in frame["content"]
    # Cap applies to the serialization helper directly too.
    d = tool_result_to_dict(ToolResult.ok(big), max_chars=100)
    assert len(d["content"]) < 200
    assert d["truncated"] is True


def test_serialize_plan_events() -> None:
    step = PlanStep(id=1, title="read", description="read a file")
    plan = Plan(goal="g", steps=[step])

    assert serialize_event(StepStart(plan=plan, step=step)) == {
        "type": "step_start",
        "step": {"id": 1, "title": "read", "description": "read a file", "status": PENDING},
    }
    step.status = IN_PROGRESS
    end = serialize_event(StepEnd(plan=plan, step=step, output="done"))
    assert end["type"] == "step_end"
    assert end["output"] == "done"
    assert end["step"]["status"] == IN_PROGRESS

    revised = serialize_event(PlanRevised(plan=plan))
    assert revised == {"type": "plan_revised", "plan": {"goal": "g", "steps": [step_to_dict(step)]}}

    step.status = DONE
    done = serialize_event(PlanDone(plan=plan))
    assert done["type"] == "plan_done"
    assert done["plan"]["goal"] == "g"
    assert done["plan"]["steps"][0]["status"] == DONE


def test_serialize_unknown_event_is_none() -> None:
    assert serialize_event(StreamEnd(response=LLMResponse(final_text="x"))) is None
    assert serialize_event(object()) is None
    assert serialize_event(None) is None


def test_message_to_dict_fields() -> None:
    m = Message.assistant(
        content="answer",
        tool_calls=[ToolCall(id="t1", name="bash", arguments='{"command": "ls"}')],
        reasoning_content="think",
    )
    d = message_to_dict(m)
    assert d["role"] == "assistant"
    assert d["content"] == "answer"
    assert d["reasoning_content"] == "think"
    assert d["tool_calls"] == [
        {"id": "t1", "name": "bash", "arguments": '{"command": "ls"}'}
    ]

    tool_msg = Message.tool("t1", "out", name="bash")
    td = message_to_dict(tool_msg)
    assert td["role"] == "tool"
    assert td["tool_call_id"] == "t1"
    assert td["name"] == "bash"
    assert td["content"] == "out"


def test_tool_call_to_dict() -> None:
    assert tool_call_to_dict(ToolCall(id="a", name="b", arguments="{}")) == {
        "id": "a",
        "name": "b",
        "arguments": "{}",
    }


def test_plan_to_dict_and_step_status() -> None:
    plan = Plan(goal="goal", steps=[PlanStep(id=2, title="t", description="d")])
    d = plan_to_dict(plan)
    assert d == {
        "goal": "goal",
        "steps": [{"id": 2, "title": "t", "description": "d", "status": PENDING}],
    }


def test_serialize_messages_roundtrip() -> None:
    msgs = [Message.system("sys"), Message.user("你好"), Message.assistant("回答")]
    out = serialize_messages(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant"]
    assert out[1]["content"] == "你好"


def test_compaction_event_serializes() -> None:
    from harness.core.runner import CompactionEvent

    frame = serialize_event(CompactionEvent("s1/transcript_0.jsonl", 3, 12_000))
    assert frame["type"] == "compacted"
    assert frame["transcript"] == "s1/transcript_0.jsonl"
    assert frame["kept"] == 3
    assert frame["freed_tokens"] == 12_000


def test_tool_result_offloaded_flag() -> None:
    from harness.core.runner import ToolResultEvent
    from harness.core.messages import ToolCall
    from harness.tools.base import ToolResult

    event = ToolResultEvent(
        ToolCall(id="c1", name="bash", arguments="{}"),
        ToolResult.ok("preview", offloaded="s1/offload_c1.txt"),
    )
    frame = serialize_event(event)
    assert frame["type"] == "tool_result"
    assert frame["offloaded"] == "s1/offload_c1.txt"


def test_tool_result_no_offloaded_key_ok() -> None:
    from harness.core.runner import ToolResultEvent
    from harness.core.messages import ToolCall
    from harness.tools.base import ToolResult

    event = ToolResultEvent(
        ToolCall(id="c2", name="bash", arguments="{}"), ToolResult.ok("tiny")
    )
    frame = serialize_event(event)
    assert frame["offloaded"] == ""
