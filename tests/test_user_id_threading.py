from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
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
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id="bt",
            result=AgentRunResult(
                backend_thread_id="bt", final_text="ok", usage=TokenUsage()
            ),
        )


def test_gateway_threads_user_id_into_resolver_user_layer():
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider(workspace_env={"ws1": {"K": "workspace"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"K": "user", "USER_ONLY": "1"}}})
    resolver = WorkspaceResolver(
        config, InMemorySecretProvider(), workspaces_root="/srv/ws",
        user_config=user_config,
    )
    gw = WorkspaceConversationGateway(resolver, convo)
    gw.handle(
        workspace_id="ws1", channel="c", external_thread_key="t", text="hi",
        backend_name="spy", user_id="u1",
    )
    assert spy.req.env["K"] == "user"          # user 层经 gateway 穿到 resolver 再到 backend
    assert spy.req.env["USER_ONLY"] == "1"
