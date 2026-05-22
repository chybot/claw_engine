# tests/test_trace_redaction_e2e.py
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.observability.memory import MemoryTracer
from tests.contract.fake_backend import FakeBackend


def test_engine_trace_metadata_uses_redacted_env_and_never_receives_plain_env_secret():
    tracer = MemoryTracer()
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    convo = ConversationService(Engine(reg, tracer=tracer), MemorySessionStore())
    config = LayeredConfigProvider(global_env={"REGION": "id"})
    secrets = InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "super-secret"}})
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws")
    gw = WorkspaceConversationGateway(resolver, convo)

    gw.handle(workspace_id="ws1", channel="webhook", external_thread_key="t",
              text="hello", backend_name="fake", message_id="m1", user_id="u1")

    assert len(tracer.traces) == 1
    rec = tracer.traces[0]
    # 全维度
    assert rec.dims.workspace_id == "ws1" and rec.dims.user_id == "u1"
    assert rec.dims.session_id is not None and rec.dims.backend_name == "fake"
    # engine-provided 强保证：trace metadata 里 env 是脱敏视图，dims/metadata 无明文 secret
    assert rec.metadata["redacted_env"]["JIRA_TOKEN"] == "***"
    assert "super-secret" not in repr(rec.dims)
    assert "super-secret" not in repr(rec.metadata)
