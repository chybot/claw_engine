from __future__ import annotations
from typing import AbstractSet, Mapping, Optional, Protocol, runtime_checkable

_REDACTED = "***"


@runtime_checkable
class SecretProvider(Protocol):
    def get_secrets(self, workspace_id: str) -> Mapping[str, str]:
        """该 workspace 的 secret(key->value)。key 视为敏感：注入 env，但日志/trace 必须脱敏。"""
        ...


class InMemorySecretProvider:
    """V1 内存 secret。真实后端(Vault/SCC)在 adapters 实现同一 Protocol。"""

    def __init__(self, secrets: Optional[Mapping[str, Mapping[str, str]]] = None) -> None:
        self._secrets = {k: dict(v) for k, v in (secrets or {}).items()}

    def get_secrets(self, workspace_id: str) -> Mapping[str, str]:
        return dict(self._secrets.get(workspace_id, {}))


def redact(env: Mapping[str, str], sensitive_keys: AbstractSet[str]) -> dict:
    """脱敏副本：sensitive_keys 的值替换为 ***（用于日志/trace；不影响实际注入的 env）。"""
    return {k: (_REDACTED if k in sensitive_keys else v) for k, v in env.items()}
