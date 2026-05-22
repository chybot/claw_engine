import dataclasses
import pytest
from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.engine.observability.noop import NoOpTracer


def test_trace_dims_frozen_and_defaults():
    d = TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="codex")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.user_id = "x"
    assert TraceDims().workspace_id is None


def test_noop_tracer_span_is_context_manager_and_noop():
    tracer = NoOpTracer()
    span = tracer.start_trace("agent-turn", dims=TraceDims(), input="hi", metadata={})
    with span as s:
        s.record_tool("shell", input={"cmd": "ls"}, output="out")
        s.finish(output="done")          # 不抛即可
    # 再次使用也不报错（幂等 no-op）
    span.finish(error="x")
