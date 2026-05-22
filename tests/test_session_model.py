import dataclasses
import pytest
from claw_engine.engine.persistence.contracts import Session

def _session(**kw):
    base = dict(session_id="s1", workspace_id="w", channel="c",
                external_thread_key="t", backend_name="fake", max_rounds=50)
    base.update(kw)
    return Session(**base)

def test_session_is_frozen():
    s = _session()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.round_count = 5

def test_with_turn_returns_new_copy_and_bumps_round():
    s = _session(round_count=0, backend_thread_id=None)
    s2 = s.with_turn(backend_thread_id="bt_1", now=123.0)
    assert s2 is not s
    assert s.round_count == 0 and s.backend_thread_id is None        # 原对象不变
    assert s2.round_count == 1 and s2.backend_thread_id == "bt_1"
    assert s2.last_active == 123.0
    assert s2.session_id == s.session_id                              # 其余字段保留

def test_defaults():
    s = _session()
    assert s.backend_thread_id is None
    assert s.round_count == 0
    assert s.last_active == 0.0
