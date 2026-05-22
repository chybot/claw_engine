# tests/test_workspace_gateway.py
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage, BackendCapabilities, BackendHealth,
)


class _SpyBackend:
    name = "spy"
    def __init__(self):
        self.req = None
    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        self.req = req
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text="ok")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt_1",
                         result=AgentRunResult(backend_thread_id="bt_1", final_text="ok", usage=TokenUsage()))


def _gateway(spy):
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider(global_env={"REGION": "global"},
                                   workspace_env={"ws1": {"REGION": "id"}})
    secrets = InMemorySecretProvider({"ws1": {"TOKEN": "t-123"}})
    specs = {"ws1": WorkspaceSpec(backend_name="spy", max_rounds=5)}
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", specs=specs)
    return WorkspaceConversationGateway(resolver, convo)


def test_gateway_resolves_cwd_env_into_backend():
    spy = _SpyBackend()
    gw = _gateway(spy)
    res = gw.handle(workspace_id="ws1", channel="c", external_thread_key="t",
                    text="hi", backend_name="fallback")
    assert res.final_text == "ok"
    # 解析出的 cwd/env（含 workspace 覆盖 + secret）真正到达 backend
    assert spy.req.cwd == "/srv/ws/ws1"
    assert spy.req.env["REGION"] == "id"
    assert spy.req.env["TOKEN"] == "t-123"

def test_gateway_uses_workspace_backend_over_fallback():
    spy = _SpyBackend()
    gw = _gateway(spy)
    # spec.backend_name="spy" 应优先于传入的 fallback
    res = gw.handle(workspace_id="ws1", channel="c", external_thread_key="t2",
                    text="hi", backend_name="fallback")
    assert res.final_text == "ok"   # 用了 spy（已注册），fallback 未注册若被用会抛错

def test_gateway_falls_back_to_passed_backend_when_spec_has_none():
    spy = _SpyBackend()
    fallback = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    reg.register_backend("fallback", lambda: fallback)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider(global_env={"REGION": "global"})
    secrets = InMemorySecretProvider({})
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", specs={})  # ws2 无 spec
    gw = WorkspaceConversationGateway(resolver, convo)
    res = gw.handle(workspace_id="ws2", channel="c", external_thread_key="t",
                    text="hi", backend_name="fallback")
    assert res.final_text == "ok"
    assert fallback.req is not None and spy.req is None   # 用了 fallback，没用 spy
