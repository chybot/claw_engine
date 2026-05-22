from __future__ import annotations
from typing import Sequence
from claw_engine.engine.runtime.contracts import AgentEvent, AgentEventKind

_TERMINAL = {AgentEventKind.TURN_COMPLETED, AgentEventKind.ERROR}


def assert_valid_event_stream(events: Sequence[AgentEvent]) -> None:
    """spec §3.3 规则 3：恰好一个终态事件，且必须在末尾；终态字段匹配。"""
    assert events, "事件流不能为空"
    terminals = [e for e in events if e.kind in _TERMINAL]
    assert len(terminals) == 1, f"必须恰好一个终态事件，实际 {len(terminals)}"
    assert events[-1].kind in _TERMINAL, "终态事件必须在末尾"
    last = events[-1]
    if last.kind is AgentEventKind.TURN_COMPLETED:
        assert last.result is not None, "TURN_COMPLETED 必须携带 result"
        assert last.error is None, "TURN_COMPLETED 不得携带 error"
    else:
        assert last.error is not None, "ERROR 必须携带 error"
        assert last.result is None, "ERROR 不得携带 result"
