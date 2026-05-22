# tests/test_workspace_resolver.py
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec

def _resolver():
    config = LayeredConfigProvider(
        global_env={"REGION": "global", "SHARED": "g"},
        workspace_env={"ws1": {"REGION": "id", "WS_ONLY": "w"}},
    )
    secrets = InMemorySecretProvider({"ws1": {"TOKEN": "t-123", "SHARED": "secret-wins"}})
    specs = {"ws1": WorkspaceSpec(allowed_skills=("abtest", "dag_tracer"),
                                  backend_name="codex", max_rounds=20)}
    return WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", specs=specs)

def test_resolution_order_global_workspace_secret():
    r = _resolver().resolve("ws1")
    # REGION: workspace 覆盖 global；WS_ONLY: 仅 workspace；TOKEN: secret；SHARED: secret 覆盖 global
    assert r.env["REGION"] == "id"
    assert r.env["WS_ONLY"] == "w"
    assert r.env["TOKEN"] == "t-123"
    assert r.env["SHARED"] == "secret-wins"

def test_cwd_is_root_join_workspace():
    r = _resolver().resolve("ws1")
    assert r.cwd == "/srv/ws/ws1"

def test_sensitive_keys_are_secret_keys():
    r = _resolver().resolve("ws1")
    assert r.sensitive_keys == frozenset({"TOKEN", "SHARED"})
    assert r.redacted_env()["TOKEN"] == "***"
    assert r.redacted_env()["SHARED"] == "***"
    assert r.redacted_env()["REGION"] == "id"            # 非敏感不打码

def test_allowed_skills_and_spec_fields():
    r = _resolver().resolve("ws1")
    assert r.allowed_skills == ("abtest", "dag_tracer")
    assert r.backend_name == "codex" and r.max_rounds == 20

def test_unknown_workspace_uses_global_and_empty_spec():
    r = _resolver().resolve("ghost")
    assert r.env == {"REGION": "global", "SHARED": "g"}
    assert r.allowed_skills == () and r.backend_name is None and r.max_rounds is None
    assert r.sensitive_keys == frozenset()

def test_repr_does_not_leak_secrets():
    r = _resolver().resolve("ws1")
    text = repr(r)
    assert "t-123" not in text and "secret-wins" not in text   # secret 明文绝不出现
    assert "'TOKEN': '***'" in text                            # 脱敏展示
    assert "'REGION': 'id'" in text                            # 非敏感明文可见

def test_path_traversal_workspace_id_rejected():
    import pytest
    from claw_engine.engine.context.workspace import InvalidWorkspaceId
    r = _resolver()
    for bad in ("../secret", "..", ".", "a/b", "a\\b", "ws/../etc"):
        with pytest.raises(InvalidWorkspaceId):
            r.resolve(bad)

def test_valid_workspace_id_with_dots_dashes_allowed():
    r = _resolver().resolve("ws.1_a-b")
    assert r.cwd == "/srv/ws/ws.1_a-b"
