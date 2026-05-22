# tests/test_engine_tracing.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentErrorKind, AgentRunResult, ToolEvent,
    TokenUsage, BackendCapabilities, BackendHealth,
)


class _ToolThenDoneBackend:
    name = "b"
    def capabilities(self):
        return BackendCapabilities(True, False, True, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        yield AgentEvent(kind=AgentEventKind.TOOL_CALL_COMPLETED,
                         tool=ToolEvent(name="shell", input={"cmd": "ls"}, output="out"))
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text="answer")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt",
                         result=AgentRunResult(backend_thread_id="bt", final_text="answer", usage=TokenUsage()))


class _ErrorBackend:
    name = "b"
    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        yield AgentEvent(kind=AgentEventKind.ERROR,
                         error=AgentError(kind=AgentErrorKind.BACKEND_CRASH, message="boom"))


def _engine(backend, tracer):
    reg = EngineRegistry()
    reg.register_backend("b", lambda: backend)
    return Engine(reg, tracer=tracer)

def test_success_turn_traced_with_dims_tools_output():
    tracer = MemoryTracer()
    eng = _engine(_ToolThenDoneBackend(), tracer)
    res = eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp", env={},
                       metadata={"workspace_id": "ws1", "user_id": "u1", "session_id": "s1"})
    assert res.final_text == "answer"
    assert len(tracer.traces) == 1
    rec = tracer.traces[0]
    assert rec.dims == TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="b")
    assert rec.input == "hi"
    assert rec.tools == [{"name": "shell", "input": {"cmd": "ls"}, "output": "out"}]
    assert rec.output == "answer" and rec.error is None

def test_failed_turn_traced_with_error_then_raises():
    tracer = MemoryTracer()
    eng = _engine(_ErrorBackend(), tracer)
    with pytest.raises(AgentRunFailed):
        eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp", env={}, metadata={})
    assert len(tracer.traces) == 1
    assert "boom" in tracer.traces[0].error and tracer.traces[0].output is None

def test_default_engine_uses_noop_tracer():
    reg = EngineRegistry()
    reg.register_backend("b", lambda: _ToolThenDoneBackend())
    eng = Engine(reg)                         # 不传 tracer -> NoOp，不报错
    assert eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp", env={}).final_text == "answer"

def test_engine_does_not_leak_env_or_backend_metadata_to_tracer():
    # 公开入口级别防泄露：env 与 backend metadata 里的 secret 都不应进 tracer
    tracer = MemoryTracer()
    eng = _engine(_ToolThenDoneBackend(), tracer)
    eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp",
                 env={"TOKEN": "super-secret"},
                 metadata={"workspace_id": "ws1", "leak": "super-secret"})  # 非 dim 字段含 secret
    rec = tracer.traces[0]
    assert "super-secret" not in repr(rec)        # env 不入 tracer；backend metadata 也不入
    assert rec.dims.workspace_id == "ws1"         # 仅 4 个安全 dim 字段进 tracer
    assert rec.metadata == {}                     # 未传 trace_metadata -> tracer metadata 为空
