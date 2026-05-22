from __future__ import annotations
from typing import Iterator
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest, AgentRunResult, TokenUsage,
    AgentError, AgentErrorKind, BackendCapabilities, BackendHealth, CodeAgentBackend,
)


class FakeBackend(CodeAgentBackend):
    name = "fake"

    def __init__(self, script: str = "ok") -> None:
        self._script = script

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_resume=True, supports_streaming=False,
            supports_tools=True, supports_mcp=False, auth_modes=("api_key",),
        )

    def healthcheck(self) -> BackendHealth:
        return BackendHealth(ok=True)

    def run(self, request: AgentRunRequest) -> Iterator[AgentEvent]:
        tid = request.backend_thread_id or "fake-thread-1"
        yield AgentEvent(kind=AgentEventKind.THREAD_STARTED, backend_thread_id=tid)
        if self._script == "error":
            yield AgentEvent(
                kind=AgentEventKind.ERROR,
                error=AgentError(kind=AgentErrorKind.BACKEND_CRASH, message="boom"),
            )
            return
        text = f"echo: {request.prompt}"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id=tid,
            result=AgentRunResult(backend_thread_id=tid, final_text=text, usage=TokenUsage()),
        )
