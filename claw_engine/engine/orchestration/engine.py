# claw_engine/engine/orchestration/engine.py
from __future__ import annotations
from typing import Any, Callable, Mapping, Optional
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.observability.contracts import TraceDims, Tracer
from claw_engine.engine.observability.noop import NoOpTracer
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentRunRequest, AgentRunResult,
)


class AgentRunFailed(RuntimeError):
    def __init__(self, error: AgentError) -> None:
        super().__init__(f"{error.kind.value}: {error.message}")
        self.error = error


class Engine:
    def __init__(self, registry: EngineRegistry, tracer: Optional[Tracer] = None) -> None:
        self._registry = registry
        self._tracer = tracer or NoOpTracer()

    def run_turn(
        self,
        *,
        backend_name: str,
        prompt: str,
        cwd: str,
        env: Mapping[str, str],
        backend_thread_id: Optional[str] = None,
        model: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,        # 给 backend / 取 dims
        trace_metadata: Optional[Mapping[str, Any]] = None,  # 给 tracer（调用方负责脱敏）
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> AgentRunResult:
        backend = self._registry.resolve_backend(backend_name)
        caps = backend.capabilities()
        md = metadata or {}
        req = AgentRunRequest(
            prompt=prompt, cwd=cwd, env=env,
            backend_thread_id=backend_thread_id if caps.supports_resume else None,
            model=model, metadata=md,
        )
        dims = TraceDims(workspace_id=md.get("workspace_id"), user_id=md.get("user_id"),
                         session_id=md.get("session_id"), backend_name=backend_name)
        result: Optional[AgentRunResult] = None
        # 关键：env 与 backend metadata 都不交给 tracer；tracer 只见 prompt / dims / trace_metadata
        with self._tracer.start_trace("agent-turn", dims=dims, input=prompt,
                                      metadata=trace_metadata) as span:
            for event in backend.run(req):
                # 能力 gate：不支持流式时忽略 delta（spec §3.3 规则 5）
                if event.kind is AgentEventKind.MESSAGE_DELTA and not caps.supports_streaming:
                    continue
                if on_event is not None:
                    on_event(event)
                if event.kind is AgentEventKind.TOOL_CALL_COMPLETED and event.tool is not None:
                    span.record_tool(event.tool.name, input=event.tool.input, output=event.tool.output)
                if event.kind is AgentEventKind.ERROR:
                    assert event.error is not None
                    span.finish(error=f"{event.error.kind.value}: {event.error.message}")
                    raise AgentRunFailed(event.error)
                if event.kind is AgentEventKind.TURN_COMPLETED:
                    assert event.result is not None
                    result = event.result
            if result is None:
                span.finish(error="backend 未产出终态 TURN_COMPLETED/ERROR")
                raise RuntimeError("backend 未产出终态 TURN_COMPLETED/ERROR，违反契约")
            span.finish(output=result.final_text)
        return result
