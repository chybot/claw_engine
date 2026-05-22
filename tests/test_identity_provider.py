import pytest
from claw_engine.engine.identity.contracts import User, UnknownUser, WorkspaceAccessDenied
from claw_engine.engine.identity.memory import InMemoryIdentityProvider

def _provider():
    users = {"ref-u1": User(user_id="u1", display_name="Alice", roles=("dev",),
                            default_workspace="ws1")}
    authorized = {"u1": ("ws1", "ws2")}
    return InMemoryIdentityProvider(users=users, authorized=authorized)

def test_resolve_user_known():
    u = _provider().resolve_user("ref-u1")
    assert u.user_id == "u1" and u.default_workspace == "ws1" and u.roles == ("dev",)

def test_resolve_user_unknown_raises():
    with pytest.raises(UnknownUser) as ei:
        _provider().resolve_user("ghost")
    assert ei.value.raw_user_ref == "ghost"

def test_authorized_workspaces_and_can_access():
    p = _provider()
    u = p.resolve_user("ref-u1")
    assert p.authorized_workspaces(u) == ("ws1", "ws2")
    assert p.can_access_workspace(u, "ws1") is True
    assert p.can_access_workspace(u, "ws9") is False

def test_workspace_access_denied_carries_context():
    e = WorkspaceAccessDenied("u1", "ws9")
    assert e.user_id == "u1" and e.workspace_id == "ws9"
