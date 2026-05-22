# tests/test_conversation_service.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.engine.orchestration.conversation import (
    ConversationService, SessionRoundsExceeded, DuplicateMessage,
)
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentErrorKind, AgentRunResult, TokenUsage,
    BackendCapabilities, BackendHealth,
)


class _SpyBackend:
    """记录每次 run 收到的 backend_thread_id；首轮返回固定 tid。"""
    name = "spy"

    def __init__(self) -> None:
        self.received: list = []
        self.calls = 0

    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))

    def healthcheck(self):
        return BackendHealth(ok=True)

    def run(self, req):
        self.calls += 1
        self.received.append(req.backend_thread_id)
        tid = req.backend_thread_id or "bt_1"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=f"echo: {req.prompt}")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id=tid,
                         result=AgentRunResult(backend_thread_id=tid, final_text=f"echo: {req.prompt}",
                                               usage=TokenUsage()))


class _FlakyBackend:
    """第一次返回 ERROR（run_turn 抛 AgentRunFailed），之后成功。"""
    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))

    def healthcheck(self):
        return BackendHealth(ok=True)

    def run(self, req):
        self.calls += 1
        if self.calls == 1:
            yield AgentEvent(kind=AgentEventKind.ERROR,
                             error=AgentError(kind=AgentErrorKind.BACKEND_CRASH, message="boom"))
            return
        tid = req.backend_thread_id or "bt_1"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text="ok")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id=tid,
                         result=AgentRunResult(backend_thread_id=tid, final_text="ok", usage=TokenUsage()))


def _service():
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    svc = ConversationService(Engine(reg), MemorySessionStore())
    return svc, spy

_KW = dict(workspace_id="w", channel="c", external_thread_key="t", cwd="/tmp", env={}, backend_name="spy")

def test_first_turn_runs_and_persists_backend_thread_id():
    svc, spy = _service()
    res = svc.handle(text="hi", **_KW)
    assert res.final_text == "echo: hi"
    assert spy.received == [None]            # 首轮无 resume 句柄

def test_second_turn_resumes_with_persisted_thread_id():
    svc, spy = _service()
    svc.handle(text="one", **_KW)
    svc.handle(text="two", **_KW)
    assert spy.received == [None, "bt_1"]    # 第二轮带上首轮回填的句柄 = 续接

def test_max_rounds_exceeded_raises():
    svc, spy = _service()
    svc.handle(text="a", max_rounds=1, **_KW)        # round_count -> 1
    with pytest.raises(SessionRoundsExceeded):
        svc.handle(text="b", max_rounds=1, **_KW)    # 已达上限，拒绝
    assert spy.calls == 1                            # 第二次未真正 run

def test_duplicate_message_id_skipped():
    svc, spy = _service()
    svc.handle(text="x", message_id="m1", **_KW)
    with pytest.raises(DuplicateMessage):
        svc.handle(text="x-again", message_id="m1", **_KW)
    assert spy.calls == 1                            # 重复消息未重复 run

def test_failed_turn_not_counted_and_message_retriable():
    flaky = _FlakyBackend()
    reg = EngineRegistry()
    reg.register_backend("flaky", lambda: flaky)
    store = MemorySessionStore()
    svc = ConversationService(Engine(reg), store)
    kw = dict(workspace_id="w", channel="c", external_thread_key="t",
              cwd="/tmp", env={}, backend_name="flaky")
    # 第一次：backend 返回 ERROR → run_turn 抛 AgentRunFailed
    with pytest.raises(AgentRunFailed):
        svc.handle(text="x", message_id="m1", **kw)
    session = store.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                                  backend_name="flaky", max_rounds=50)
    assert session.round_count == 0                          # 失败轮不计数
    assert not store.is_processed(session.session_id, "m1")  # 未标记 processed
    # 第二次同 message_id：应再次触发 backend（非 DuplicateMessage），证明可重试
    res = svc.handle(text="x", message_id="m1", **kw)
    assert res.final_text == "ok"
    assert flaky.calls == 2
