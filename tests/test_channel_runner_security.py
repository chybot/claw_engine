import json
import pytest
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
from claw_engine.engine.channels.contracts import InboundAuthError
from claw_engine.adapters.channels.webhook import WebhookChannel
from claw_engine.engine.runtime.contracts import (
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    TokenUsage,
    BackendCapabilities,
    BackendHealth,
)

SECRET = "s3cr3t"


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
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id="bt",
            result=AgentRunResult(
                backend_thread_id="bt",
                final_text="ok",
                usage=TokenUsage(),
            ),
        )


def test_bad_signature_blocks_loop_no_backend_no_reply():
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    resolver = WorkspaceResolver(
        LayeredConfigProvider(),
        InMemorySecretProvider(),
        workspaces_root="/srv/ws",
    )
    ws_gateway = WorkspaceConversationGateway(resolver, convo)
    channel = WebhookChannel(SECRET)
    runner = ChannelRunner(
        channel,
        StaticWorkspaceRouter("ws1"),
        ws_gateway,
        default_backend_name="spy",
    )

    body = json.dumps({"user": "u1", "thread": "t1", "text": "hello"})
    with pytest.raises(InboundAuthError):
        runner.handle_raw(body, {"X-Signature": "WRONG"})

    assert spy.req is None           # 未通过校验 → backend 从未运行
    assert channel.sent_texts == []  # 没有任何回复发出
