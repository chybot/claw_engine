# tests/test_engine_run_turn.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.engine.runtime.contracts import AgentErrorKind
from tests.contract.fake_backend import FakeBackend

def _engine():
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    reg.register_backend("fake_err", lambda: FakeBackend(script="error"))
    return Engine(reg)

def test_run_turn_success_returns_result():
    res = _engine().run_turn(backend_name="fake", prompt="hello", cwd="/tmp", env={})
    assert res.final_text == "echo: hello"
    assert res.backend_thread_id == "fake-thread-1"

def test_run_turn_error_raises_typed():
    with pytest.raises(AgentRunFailed) as ei:
        _engine().run_turn(backend_name="fake_err", prompt="x", cwd="/tmp", env={})
    assert ei.value.error.kind is AgentErrorKind.BACKEND_CRASH

def test_run_turn_collects_events_via_callback():
    seen = []
    res = _engine().run_turn(
        backend_name="fake", prompt="hi", cwd="/tmp", env={}, on_event=seen.append
    )
    kinds = [e.kind.value for e in seen]
    assert kinds == ["thread_started", "message_completed", "turn_completed"]
    assert res.final_text == "echo: hi"
