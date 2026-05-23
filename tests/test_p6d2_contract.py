# tests/test_p6d2_contract.py
import dataclasses
import pytest
from claw_engine.engine.identity.contracts import Principal, User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec

def test_principal_is_frozen_with_user_and_workspace():
    p = Principal(user=User(user_id="u1", display_name="Alice"), workspace_id="ws1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.workspace_id = "x"
    assert p.user.user_id == "u1" and p.workspace_id == "ws1"

def test_can_run_workflow_default_allow_and_denied():
    idp = InMemoryIdentityProvider(
        users={"ref-u": User(user_id="u", display_name="A", default_workspace="ws1")},
        authorized={"u": ("ws1",)},
        denied_workflows={"u": ("scary-wf",)},
    )
    u = idp.resolve_user("ref-u")
    assert idp.can_run_workflow(u, "ws1", "scan_logistics") is True   # default-allow
    assert idp.can_run_workflow(u, "ws1", "scary-wf") is False        # explicit deny

def test_workspace_spec_and_resolved_carry_allowed_workflows():
    spec = WorkspaceSpec(allowed_skills=("abtest",), allowed_workflows=("wf2", "wf3"),
                         backend_name="codex", max_rounds=10)
    assert spec.allowed_workflows == ("wf2", "wf3")
    resolver = WorkspaceResolver(
        LayeredConfigProvider(), InMemorySecretProvider(),
        workspaces_root="/srv/ws", specs={"ws1": spec},
    )
    rw = resolver.resolve("ws1")
    assert rw.allowed_workflows == ("wf2", "wf3")
    assert rw.allowed_skills == ("abtest",)                           # 既有不回归

def test_resolved_workspace_default_allowed_workflows_empty():
    resolver = WorkspaceResolver(
        LayeredConfigProvider(), InMemorySecretProvider(), workspaces_root="/srv/ws",
    )
    rw = resolver.resolve("ghost")
    assert rw.allowed_workflows == ()                                 # 默认空 tuple
