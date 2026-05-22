# tests/test_memory_tracer.py
from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.engine.observability.memory import MemoryTracer


def test_records_dims_input_tools_and_output():
    tracer = MemoryTracer()
    dims = TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="codex")
    with tracer.start_trace("agent-turn", dims=dims, input="hi", metadata={"k": "v"}) as span:
        span.record_tool("shell", input={"cmd": "ls"}, output="out")
        span.finish(output="done")
    assert len(tracer.traces) == 1
    rec = tracer.traces[0]
    assert rec.name == "agent-turn" and rec.dims == dims
    assert rec.input == "hi" and rec.metadata == {"k": "v"}
    assert rec.tools == [{"name": "shell", "input": {"cmd": "ls"}, "output": "out"}]
    assert rec.output == "done" and rec.error is None and rec.finished is True


def test_records_error():
    tracer = MemoryTracer()
    with tracer.start_trace("agent-turn", dims=TraceDims(), input="x") as span:
        span.finish(error="boom")
    assert tracer.traces[0].error == "boom" and tracer.traces[0].output is None


def test_exit_without_finish_auto_finishes():
    tracer = MemoryTracer()
    with tracer.start_trace("agent-turn", dims=TraceDims(), input="x"):
        pass                                  # 未显式 finish
    assert tracer.traces[0].finished is True   # __exit__ 兜底 finish


def test_finish_is_idempotent():
    tracer = MemoryTracer()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    span.finish(output="first")
    span.finish(output="second")              # 第二次忽略
    assert tracer.traces[0].output == "first"
