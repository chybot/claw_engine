# tests/test_user_config_layer.py
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver

def _resolver():
    config = LayeredConfigProvider(global_env={"REGION": "global", "TIER": "g"},
                                   workspace_env={"ws1": {"REGION": "id"}})
    secrets = InMemorySecretProvider({"ws1": {"TOKEN": "t-1", "TIER": "secret"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"TIER": "user", "USER_ONLY": "x"}}})
    return WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", user_config=user_config)

def test_user_layer_overrides_workspace_but_secret_wins():
    r = _resolver().resolve("ws1", user_id="u1")
    assert r.env["REGION"] == "id"            # workspace 覆盖 global
    assert r.env["USER_ONLY"] == "x"          # user 独有
    # TIER: global=g -> (workspace 无) -> user=user -> secret=secret；secret 最高
    assert r.env["TIER"] == "secret"
    assert r.env["TOKEN"] == "t-1"

def test_no_user_id_skips_user_layer():
    r = _resolver().resolve("ws1")            # 不传 user_id -> 无 user 层（向后兼容）
    assert "USER_ONLY" not in r.env
    assert r.env["TIER"] == "secret"          # secret 仍最高

def test_user_layer_between_workspace_and_secret():
    # 构造一个无 secret 冲突的 key 验证 user 覆盖 workspace
    config = LayeredConfigProvider(workspace_env={"ws1": {"K": "workspace"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"K": "user"}}})
    r = WorkspaceResolver(config, InMemorySecretProvider(), workspaces_root="/srv/ws",
                          user_config=user_config)
    assert r.resolve("ws1", user_id="u1").env["K"] == "user"   # user 覆盖 workspace
