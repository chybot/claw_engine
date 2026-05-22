# tests/test_secret_injection_e2e.py
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec


def test_secret_reaches_env_but_redacted_view_masks_it():
    config = LayeredConfigProvider(global_env={"REGION": "id"})
    secrets = InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "super-secret"}})
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws",
                                 specs={"ws1": WorkspaceSpec()})
    rw = resolver.resolve("ws1")

    # 实际注入 env 含真实 secret（供子进程/skill 通过 env 读取——不落 skill 源码）
    assert rw.env["JIRA_TOKEN"] == "super-secret"
    # 脱敏视图（用于日志/trace）打码，且不含明文
    redacted = rw.redacted_env()
    assert redacted["JIRA_TOKEN"] == "***"
    assert "super-secret" not in repr(redacted)
    # 非敏感字段在两边都明文
    assert rw.env["REGION"] == "id" and redacted["REGION"] == "id"
