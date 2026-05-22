from claw_engine.engine.runtime.contract_suite import assert_valid_event_stream
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind
from tests.contract.fake_backend import FakeBackend

def test_success_stream_is_contract_valid():
    backend = FakeBackend(script="ok")
    req = AgentRunRequest(prompt="hello", cwd="/tmp", env={})
    events = list(backend.run(req))
    assert_valid_event_stream(events)  # 不抛即合规
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "echo: hello"

def test_error_stream_is_contract_valid():
    backend = FakeBackend(script="error")
    req = AgentRunRequest(prompt="x", cwd="/tmp", env={})
    events = list(backend.run(req))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR

def test_double_terminal_is_rejected():
    import pytest
    from claw_engine.engine.runtime.contracts import AgentEvent, AgentEventKind, AgentRunResult, TokenUsage
    bad = [
        AgentEvent(kind=AgentEventKind.TURN_COMPLETED,
                   result=AgentRunResult(backend_thread_id=None, final_text="a", usage=TokenUsage())),
        AgentEvent(kind=AgentEventKind.TURN_COMPLETED,
                   result=AgentRunResult(backend_thread_id=None, final_text="b", usage=TokenUsage())),
    ]
    with pytest.raises(AssertionError):
        assert_valid_event_stream(bad)
