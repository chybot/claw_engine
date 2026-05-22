# claw_engine/engine/orchestration/engine.py
from __future__ import annotations
from typing import Any, Callable, Mapping, Optional
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentRunRequest, AgentRunResult,
)


class AgentRunFailed(RuntimeError):
    def __init__(self, error: AgentError) -> None:
        super().__init__(f"{error.kind.value}: {error.message}")
        self.error = error


class Engine:
    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry

    def run_turn(
        self,
        *,
        backend_name: str,
        prompt: str,
        cwd: str,
        env: Mapping[str, str],
        backend_thread_id: Optional[str] = None,
        model: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> AgentRunResult:
        backend = self._registry.resolve_backend(backend_name)
        caps = backend.capabilities()
        req = AgentRunRequest(
            prompt=prompt, cwd=cwd, env=env,
            backend_thread_id=backend_thread_id if caps.supports_resume else None,
            model=model, metadata=metadata or {},
        )
        result: Optional[AgentRunResult] = None
        for event in backend.run(req):
            # 能力 gate：不支持流式时忽略 delta（spec §3.3 规则 5）
            if event.kind is AgentEventKind.MESSAGE_DELTA and not caps.supports_streaming:
                continue
            if on_event is not None:
                on_event(event)
            if event.kind is AgentEventKind.ERROR:
                assert event.error is not None
                raise AgentRunFailed(event.error)
            if event.kind is AgentEventKind.TURN_COMPLETED:
                assert event.result is not None
                result = event.result
        if result is None:
            raise RuntimeError("backend 未产出终态 TURN_COMPLETED/ERROR，违反契约")
        return result
