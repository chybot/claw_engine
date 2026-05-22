# tests/test_channel_runner_e2e.py
import hashlib
import hmac
import json
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.channels.routing import StaticWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.engine.channels.contracts import ReplyTarget
from claw_engine.adapters.channels.webhook import WebhookChannel
from tests.contract.fake_backend import FakeBackend

SECRET = "s3cr3t"


def _sig(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def _runner(channel):
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    convo = ConversationService(Engine(reg), MemorySessionStore())
    resolver = WorkspaceResolver(LayeredConfigProvider(), InMemorySecretProvider(),
                                 workspaces_root="/srv/ws")
    ws_gateway = WorkspaceConversationGateway(resolver, convo)
    router = StaticWorkspaceRouter("ws1")
    return ChannelRunner(channel, router, ws_gateway, default_backend_name="fake")


def test_raw_to_reply_loop():
    channel = WebhookChannel(SECRET)
    runner = _runner(channel)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "hello", "message_id": "m1"})
    result = runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert result.final_text == "echo: hello"
    target = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    assert channel.sent_texts == [(target, "echo: hello")]


def test_static_router_returns_fixed_workspace():
    from claw_engine.engine.channels.contracts import IncomingMessage
    r = StaticWorkspaceRouter("wsX")
    msg = IncomingMessage(channel="webhook", raw_user_ref="u", external_thread_key="t", text="hi")
    assert r.route(msg) == "wsX"
