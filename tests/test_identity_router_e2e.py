# tests/test_identity_router_e2e.py
import hashlib
import hmac
import json
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.identity.contracts import (
    User, UnknownUser, WorkspaceAccessDenied, WorkspaceRouteError,
)
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.channels.identity_routing import IdentityWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.adapters.channels.webhook import WebhookChannel
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage, BackendCapabilities, BackendHealth,
)

SECRET = "s3cr3t"

def _sig(body):
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


class _EnvEchoBackend:
    """捕获 req（含 env）并 echo，用于证明 user config 穿到了 backend env。"""
    name = "fake"

    def __init__(self):
        self.req = None

    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))

    def healthcheck(self):
        return BackendHealth(ok=True)

    def run(self, req):
        self.req = req
        text = f"echo: {req.prompt}"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt",
                         result=AgentRunResult(backend_thread_id="bt", final_text=text, usage=TokenUsage()))


def _runner(channel, *, authorized_ws, user_default="ws1"):
    spy = _EnvEchoBackend()
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider()
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"USER_TAG": "alice"}}})
    resolver = WorkspaceResolver(config, InMemorySecretProvider(), workspaces_root="/srv/ws",
                                 user_config=user_config)
    gw = WorkspaceConversationGateway(resolver, convo)
    identity = InMemoryIdentityProvider(
        users={"ref-u1": User(user_id="u1", display_name="Alice", default_workspace=user_default)},
        authorized={"u1": authorized_ws},
    )
    router = IdentityWorkspaceRouter(identity)
    return ChannelRunner(channel, router, gw, default_backend_name="fake"), spy

def _body(user_ref):
    return json.dumps({"user": user_ref, "thread": "t1", "text": "hello"})

def test_authorized_user_routes_replies_and_user_config_reaches_backend():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws1",))
    body = _body("ref-u1")
    result = runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert result.final_text == "echo: hello"
    assert channel.sent_texts[-1][1] == "echo: hello"
    assert spy.req.env["USER_TAG"] == "alice"           # user 层 config 真正穿到 backend env（闭环）

def test_unauthorized_workspace_denied_no_backend_no_reply():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws2",))   # 默认 ws1 不在授权内
    body = _body("ref-u1")
    with pytest.raises(WorkspaceAccessDenied):
        runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert spy.req is None
    assert channel.sent_texts == []      # 越权 -> backend 不跑、无回复

def test_unknown_user_raises_no_backend_no_reply():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws1",))
    body = _body("ghost")
    with pytest.raises(UnknownUser):
        runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert spy.req is None
    assert channel.sent_texts == []

def test_no_default_workspace_raises_route_error():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws1",), user_default=None)
    body = _body("ref-u1")
    with pytest.raises(WorkspaceRouteError):
        runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert spy.req is None
    assert channel.sent_texts == []      # 无法路由 != 越权；同样不进引擎
