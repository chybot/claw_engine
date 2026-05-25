# demo.py — run with: .venv/bin/python demo.py
import hashlib
import hmac
import json
import tempfile
from pathlib import Path

# --- engine pieces ---------------------------------------------------------
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage,
    BackendCapabilities, BackendHealth,
)
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway

from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
from claw_engine.engine.identity.contracts import Principal, User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.engine.channels.identity_routing import IdentityWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner

from claw_engine.engine.skills.source import LocalDirSkillSource
from claw_engine.engine.skills.provisioner import SkillProvisioner

from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService
from claw_engine.engine.workflows.bridge import WorkflowToolBridge, WorkflowNotAllowed

# --- one adapter --------------------------------------------------------------
from claw_engine.adapters.channels.webhook import WebhookChannel


# --- a tiny inline echo backend (so the demo needs no real codex/claude) ----
class EchoBackend:
    name = "fake"
    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        text = f"echo: {req.prompt}"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt-1",
            result=AgentRunResult(backend_thread_id="bt-1", final_text=text, usage=TokenUsage()),
        )


def main(tmp_path: Path):
    # 1. -------- Identity (with skill + workflow denies for demonstration) ---
    identity = InMemoryIdentityProvider(
        users={"alice@example.com": User(
            user_id="u1", display_name="Alice", roles=("dev",),
            default_workspace="ws1",
        )},
        authorized={"u1": ("ws1",)},
        denied_skills={"u1": ("dangerous-skill",)},
        denied_workflows={"u1": ()},
    )

    # 2. -------- Context: config layers + secrets ---------------------------
    config = LayeredConfigProvider(
        global_env={"REGION": "global", "TIER": "g"},
        workspace_env={"ws1": {"REGION": "id"}},
    )
    secrets = InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "PLAINTEXT-SECRET-DO-NOT-LEAK"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"USER_TAG": "alice"}}})

    resolver = WorkspaceResolver(
        config, secrets,
        workspaces_root=str(tmp_path / "workspaces"),
        specs={"ws1": WorkspaceSpec(
            allowed_skills=("abtest", "dag_tracer"),
            allowed_workflows=("scan_logistics",),
            backend_name="fake", max_rounds=20,
        )},
        user_config=user_config,
    )
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True)   # cwd must exist for skill provisioning

    ws = resolver.resolve("ws1", user_id="u1")
    print("=== ResolvedWorkspace ===")
    print(ws)                                                # __repr__ shows REDACTED env
    assert ws.env["JIRA_TOKEN"] == "PLAINTEXT-SECRET-DO-NOT-LEAK"   # but real env still has it
    assert "PLAINTEXT-SECRET" not in repr(ws)                       # repr is redacted

    # 3. -------- Engine + Tracer + Conversation + Gateway -------------------
    spy = EchoBackend()
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: spy)

    tracer = MemoryTracer()
    engine = Engine(reg, tracer=tracer)

    session_store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    convo = ConversationService(engine, session_store)
    gateway = WorkspaceConversationGateway(resolver, convo)

    # 4. -------- Inbound channel (HMAC webhook) + identity-based routing ----
    secret = "wh-secret"
    channel = WebhookChannel(secret)
    router = IdentityWorkspaceRouter(identity)
    runner = ChannelRunner(channel, router, gateway, default_backend_name="fake")

    def sign(body): return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    body = json.dumps({
        "user": "alice@example.com", "thread": "thread-A",
        "text": "hello world", "message_id": "m1",
    })
    result = runner.handle_raw(body, {"X-Signature": sign(body)})
    print("\n=== Reply ===\n", result.final_text)
    print("\n=== Captured outbound ===\n", channel.sent_texts[-1])

    # 5. -------- Trace dimensions + redaction invariant ---------------------
    rec = tracer.traces[0]
    print("\n=== Trace ===")
    print(" name           :", rec.name)
    print(" dims           :", rec.dims)
    print(" tools          :", rec.tools)
    print(" redacted_env   :", rec.metadata.get("redacted_env"))
    assert "PLAINTEXT-SECRET" not in repr(rec.metadata), "secret leaked into trace metadata!"
    assert "PLAINTEXT-SECRET" not in repr(rec.dims),     "secret leaked into trace dims!"
    print(" leak check     : ok — no plaintext secret anywhere in trace dims/metadata")

    # 6. -------- Skill provisioning with RBAC filter ------------------------
    skills_src_root = tmp_path / "skills_src"
    for name in ("abtest", "dag_tracer", "dangerous-skill"):
        (skills_src_root / name).mkdir(parents=True)
        (skills_src_root / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

    provisioner = SkillProvisioner(LocalDirSkillSource(str(skills_src_root)), identity)
    pr = provisioner.provision(ws, identity.resolve_user("alice@example.com"))
    print("\n=== SkillProvisioner result ===")
    print(" provisioned :", pr.provisioned)
    print(" denied      :", pr.denied)
    print(" missing     :", pr.missing)
    print(" revoked     :", pr.revoked)
    # `dangerous-skill` is NOT in workspace.allowed_skills -> ignored entirely.
    # Within allowed_skills, both abtest and dag_tracer pass can_use_skill, so they're provisioned.

    # 7. -------- Workflow bridge (RBAC + cross-workspace isolation) ---------
    def scan_logistics(params, ctx):
        return {"scanned": params.get("region", "id"), "items": 1048}

    wfreg = EngineRegistry()
    wfreg.register_workflow("scan_logistics", scan_logistics)
    wfreg.register_workflow("forbidden_wf",   lambda p, c: "should not run")

    wfservice = WorkflowService(wfreg, MemoryWorkflowStore())
    bridge = WorkflowToolBridge(wfservice, identity, resolver)

    user = identity.resolve_user("alice@example.com")
    principal = Principal(user=user, workspace_id="ws1")

    inv = bridge.invoke("scan_logistics", {"region": "id"}, principal)
    print("\n=== Workflow bridge ===")
    print(" invoke -> :", inv)
    fetched = bridge.get(inv.run_id, principal)
    print(" get    -> status:", fetched.status, "result:", fetched.result,
          "owner_workspace_id:", fetched.owner_workspace_id)

    # 8. -------- RBAC enforcement (forbidden_wf NOT in workspace.allowed_workflows)
    try:
        bridge.invoke("forbidden_wf", {}, principal)
    except WorkflowNotAllowed as e:
        print(" invoke forbidden_wf -> WorkflowNotAllowed:", e)

    # 9. -------- Revocation symmetry: invoke succeeded, then revoke can_run, then get fails
    identity._denied_workflows["u1"] = {"scan_logistics"}   # V1 InMemory allows direct mutation
    try:
        bridge.get(inv.run_id, principal)
    except WorkflowNotAllowed as e:
        print(" after revoke, get old run -> WorkflowNotAllowed:", e,
              "(revocation symmetric with provisioning — old artifacts stop working)")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        main(Path(t))

