from claw_engine.engine.identity.contracts import User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider


def _provider():
    users = {"ref-u1": User(user_id="u1", display_name="Alice", default_workspace="ws1")}
    return InMemoryIdentityProvider(
        users=users, authorized={"u1": ("ws1",)},
        denied_skills={"u1": ("dangerous-skill",)},
    )


def test_can_use_skill_default_allow():
    p = _provider()
    u = p.resolve_user("ref-u1")
    assert p.can_use_skill(u, "ws1", "abtest") is True          # 未 deny -> 允许（default-allow）


def test_can_use_skill_denied():
    p = _provider()
    u = p.resolve_user("ref-u1")
    assert p.can_use_skill(u, "ws1", "dangerous-skill") is False  # 显式 deny


def test_can_use_skill_user_without_denies_allows_all():
    p = InMemoryIdentityProvider(
        users={"ref-u2": User(user_id="u2", display_name="Bob")},
        authorized={"u2": ("ws1",)},
    )
    u = p.resolve_user("ref-u2")
    assert p.can_use_skill(u, "ws1", "anything") is True
