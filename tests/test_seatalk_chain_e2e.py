# tests/test_seatalk_chain_e2e.py
import hashlib
import json
import shutil
from pathlib import Path
import pytest

from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
from claw_engine.engine.identity.contracts import (
    User, WorkspaceAccessDenied,
)
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.engine.channels.identity_routing import IdentityWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.engine.skills.source import LocalDirSkillSource
from claw_engine.engine.skills.provisioner import SkillProvisioner
from claw_engine.adapters.channels.seatalk import SeaTalkChannel
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage,
    BackendCapabilities, BackendHealth,
)

SECRET = "seatalk-shared-secret"
ALGO_BOT_SKILLS = Path("/Users/lucas.xu/Desktop/work/git/algo-bot-skills/algo")
REAL_SKILL_NAME = "dag_tracer"


def _sig(body):
    return hashlib.sha256((body + SECRET).encode()).hexdigest()


def _payload(text="hello", email="alice@shopee.com", thread="thread-A", msg_id="m1"):
    return json.dumps({
        "event_type": "message_from_bot_subscriber",
        "event": {
            "message": {"text": {"plain_text": text}, "tag": "text"},
            "sender": {"email": email},
            "thread_id": thread,
            "message_id": msg_id,
        },
    })


class _SpyBackend:
    """捕获 req 用于断言（prompt/cwd/env 真传到 backend）。"""

    name = "spy"

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
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id="bt-st",
            result=AgentRunResult(
                backend_thread_id="bt-st",
                final_text=text,
                usage=TokenUsage(),
            ),
        )


def _build_runner(tmp_path, *, authorized=("ws1",)):
    """构造完整 SeaTalk 链；spy backend + MemoryTracer 供断言。"""
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)

    tracer = MemoryTracer()
    engine = Engine(reg, tracer=tracer)
    convo = ConversationService(engine, MemorySessionStore())

    workspaces_root = tmp_path / "workspaces"
    cwd = workspaces_root / "ws1"
    cwd.mkdir(parents=True)

    resolver = WorkspaceResolver(
        LayeredConfigProvider(
            global_env={"REGION": "global"},
            workspace_env={"ws1": {"REGION": "id"}},
        ),
        InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "PLAIN-SECRET"}}),
        workspaces_root=str(workspaces_root),
        specs={"ws1": WorkspaceSpec(
            allowed_skills=("dag_tracer",),
            backend_name="spy",
            max_rounds=20,
        )},
    )
    gateway = WorkspaceConversationGateway(resolver, convo)

    identity = InMemoryIdentityProvider(
        users={"alice@shopee.com": User(
            user_id="u1",
            display_name="Alice",
            default_workspace="ws1",
        )},
        authorized={"u1": tuple(authorized)},
    )
    router = IdentityWorkspaceRouter(identity)

    channel = SeaTalkChannel(SECRET)
    runner = ChannelRunner(channel, router, gateway, default_backend_name="spy")
    return runner, channel, spy, tracer, identity, resolver, cwd


def test_happy_path_full_seatalk_chain(tmp_path):
    """SeaTalk payload -> Channel -> Identity -> Workspace -> spy backend -> reply。
    断言每层都把信息按预期传到该传的位置：真 secret 走 backend env，脱敏走 trace。"""
    runner, channel, spy, tracer, _, _, cwd = _build_runner(tmp_path)
    body = _payload(text="hello world")

    result = runner.handle_raw(body, {"Signature": _sig(body)})

    # 链路终点：reply 已捕获到 channel
    assert result.final_text == "echo: hello world"
    assert len(channel.sent_texts) == 1
    target, text = channel.sent_texts[0]
    assert target.channel == "seatalk"
    assert target.external_thread_key == "thread-A"
    assert target.raw_user_ref == "alice@shopee.com"
    assert text == "echo: hello world"

    # backend 真的收到了通过整条链解析过的数据
    assert spy.req is not None
    assert spy.req.prompt == "hello world"          # SeaTalk plain_text -> prompt
    assert spy.req.cwd == str(cwd)                  # ResolvedWorkspace.cwd
    assert spy.req.env["REGION"] == "id"            # workspace 覆盖 global
    assert spy.req.env["JIRA_TOKEN"] == "PLAIN-SECRET"  # 真 secret 走 backend

    # 同时：trace 看到的 env 是脱敏的（V1 P6c 不变式）
    rec = tracer.traces[0]
    assert rec.dims.workspace_id == "ws1"
    assert rec.dims.user_id == "u1"
    assert rec.dims.session_id is not None
    assert rec.dims.backend_name == "spy"
    assert rec.metadata["redacted_env"]["JIRA_TOKEN"] == "***"
    assert "PLAIN-SECRET" not in repr(rec.metadata)
    assert "PLAIN-SECRET" not in repr(rec.dims)


def test_bad_signature_blocks_seatalk_chain(tmp_path):
    """verify-before-engine invariant 在 SeaTalk 链上同样成立（镜像 P6a webhook 安全测试）：
    坏签名 -> InboundAuthError，backend 不跑，channel 无回复。"""
    from claw_engine.engine.channels.contracts import InboundAuthError
    runner, channel, spy, _, _, _, _ = _build_runner(tmp_path)
    body = _payload(text="hello")
    with pytest.raises(InboundAuthError):
        runner.handle_raw(body, {"Signature": "wrong-digest"})
    assert spy.req is None
    assert channel.sent_texts == []


def test_rbac_unauthorized_user_no_backend_no_reply(tmp_path):
    """用户被移出 workspace 授权 -> Identity 路由抛 WorkspaceAccessDenied；
    backend 从未运行，channel 无回复。"""
    runner, channel, spy, _, _, _, _ = _build_runner(tmp_path, authorized=())
    body = _payload(text="hello")
    with pytest.raises(WorkspaceAccessDenied):
        runner.handle_raw(body, {"Signature": _sig(body)})
    assert spy.req is None              # backend 完全没跑
    assert channel.sent_texts == []     # 无回复发出


# --- skill provisioning 双层 proof：fixture 必跑 + 真路径 enhanced ---

FIXTURE_DAG_TRACER = Path(__file__).parent / "fixtures" / "algo_skills" / "dag_tracer"


def test_provision_fixture_dag_tracer_skill_into_workspace(tmp_path):
    """**默认必跑** proof：用 repo 里的 fixture（从真 algo-bot-skills 抽取最小副本），
    证明 V1 SkillProvisioner 能处理真实 SKILL.md 结构 + 落盘 + manifest。
    不依赖任何本机外部路径——这是核心 portability proof。"""
    runner, _, _, _, identity, resolver, cwd = _build_runner(tmp_path)

    src_root = tmp_path / "skills_src"
    src_root.mkdir()
    shutil.copytree(FIXTURE_DAG_TRACER, src_root / "dag_tracer")

    provisioner = SkillProvisioner(LocalDirSkillSource(str(src_root)), identity)
    user = identity.resolve_user("alice@shopee.com")
    ws = resolver.resolve("ws1", user_id="u1")
    result = provisioner.provision(ws, user)

    assert result.provisioned == ("dag_tracer",)
    assert result.denied == () and result.missing == () and result.invalid == ()

    dst = cwd / ".agents" / "skills" / "dag_tracer"
    assert (dst / "SKILL.md").is_file()
    assert (dst / "scripts" / "diagnose_search_trace.py").is_file()
    # SKILL.md 真有 frontmatter（不是空文件）
    content = (dst / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "name: dag_tracer" in content


@pytest.mark.skipif(
    not (ALGO_BOT_SKILLS / REAL_SKILL_NAME).exists(),
    reason=f"algo-bot-skills not available at {ALGO_BOT_SKILLS} — fixture test above is the must-run proof",
)
def test_provision_real_dag_tracer_skill_into_workspace(tmp_path):
    """**enhanced** proof：本机有 algo-bot-skills 时额外跑真路径。
    跳过不影响 P7 验收——上面 fixture 测试已经是 must-run proof。"""
    runner, _, _, _, identity, resolver, cwd = _build_runner(tmp_path)

    src_root = tmp_path / "skills_src"
    src_root.mkdir()
    shutil.copytree(ALGO_BOT_SKILLS / REAL_SKILL_NAME, src_root / REAL_SKILL_NAME)

    provisioner = SkillProvisioner(LocalDirSkillSource(str(src_root)), identity)
    user = identity.resolve_user("alice@shopee.com")
    ws = resolver.resolve("ws1", user_id="u1")
    result = provisioner.provision(ws, user)

    assert result.provisioned == ("dag_tracer",)
    dst = cwd / ".agents" / "skills" / "dag_tracer"
    assert (dst / "SKILL.md").is_file()
    assert (dst / "scripts" / "diagnose_search_trace.py").is_file()
    content = (dst / "SKILL.md").read_text(encoding="utf-8")
    assert len(content) > 100   # 真 SKILL.md 内容显著大于 fixture
