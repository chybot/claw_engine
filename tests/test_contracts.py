# tests/test_contracts.py
import dataclasses
import pytest
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest, AgentRunResult,
    AgentError, AgentErrorKind, BackendCapabilities, ToolEvent, TokenUsage,
)

def test_request_is_frozen():
    req = AgentRunRequest(prompt="hi", cwd="/tmp", env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.prompt = "x"

def test_request_defaults():
    req = AgentRunRequest(prompt="hi", cwd="/tmp", env={})
    assert req.backend_thread_id is None
    assert req.timeout_s == 1800

def test_turn_completed_carries_result():
    res = AgentRunResult(backend_thread_id="t1", final_text="done", usage=TokenUsage())
    ev = AgentEvent(kind=AgentEventKind.TURN_COMPLETED, result=res)
    assert ev.kind is AgentEventKind.TURN_COMPLETED
    assert ev.result.final_text == "done"
    assert ev.result.status == "ok"

def test_error_event_carries_error():
    err = AgentError(kind=AgentErrorKind.TIMEOUT, message="slow", retriable=True)
    ev = AgentEvent(kind=AgentEventKind.ERROR, error=err)
    assert ev.error.kind is AgentErrorKind.TIMEOUT

def test_capabilities_fields():
    caps = BackendCapabilities(
        supports_resume=True, supports_streaming=False,
        supports_tools=True, supports_mcp=False, auth_modes=("api_key",),
    )
    assert caps.supports_resume and not caps.supports_streaming
