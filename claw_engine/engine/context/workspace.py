# claw_engine/engine/context/workspace.py
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional
from claw_engine.engine.context.config import ConfigProvider
from claw_engine.engine.context.secrets import SecretProvider, redact
from claw_engine.engine.context.user_config import UserConfigProvider

_WORKSPACE_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")


class InvalidWorkspaceId(ValueError):
    pass


def _validate_workspace_id(workspace_id: str) -> None:
    """只允许 [A-Za-z0-9_.-]+，且禁止 '.'/'..'，防路径穿越（../x、a/b、a\\b 等一律拒绝）。"""
    if workspace_id in (".", "..") or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
        raise InvalidWorkspaceId(f"非法 workspace_id（可能路径穿越）: {workspace_id!r}")


@dataclass(frozen=True)
class WorkspaceSpec:
    """每 workspace 的静态配置（V1 内存；真实来源由 adapters 提供）。"""
    allowed_skills: tuple[str, ...] = ()
    backend_name: Optional[str] = None
    max_rounds: Optional[int] = None


@dataclass(frozen=True, repr=False)
class ResolvedWorkspace:
    workspace_id: str
    cwd: str
    env: Mapping[str, str]                 # global ⊕ workspace ⊕ secrets，注入子进程
    sensitive_keys: frozenset[str]         # env 中来自 secret 的 key（脱敏依据）
    allowed_skills: tuple[str, ...] = ()   # skill visibility 数据模型（provisioning 见 P6）
    backend_name: Optional[str] = None
    max_rounds: Optional[int] = None

    def redacted_env(self) -> dict:
        return redact(self.env, self.sensitive_keys)

    def __repr__(self) -> str:
        # 自定义 repr：绝不泄露 env 明文 secret，只展示脱敏后的 env（repr=False 关掉默认实现）
        return (f"ResolvedWorkspace(workspace_id={self.workspace_id!r}, cwd={self.cwd!r}, "
                f"env={self.redacted_env()!r}, sensitive_keys={set(self.sensitive_keys)!r}, "
                f"allowed_skills={self.allowed_skills!r}, backend_name={self.backend_name!r}, "
                f"max_rounds={self.max_rounds!r})")


class WorkspaceResolver:
    """workspace_id -> ResolvedWorkspace。解析顺序见计划「设计基线」。"""

    def __init__(self, config: ConfigProvider, secrets: SecretProvider, *,
                 workspaces_root: str,
                 specs: Optional[Mapping[str, WorkspaceSpec]] = None,
                 user_config: Optional[UserConfigProvider] = None) -> None:
        self._config = config
        self._secrets = secrets
        self._root = workspaces_root
        self._specs = dict(specs or {})
        self._user_config = user_config

    def resolve(self, workspace_id: str, user_id: Optional[str] = None) -> ResolvedWorkspace:
        _validate_workspace_id(workspace_id)                        # 防路径穿越
        base_env = dict(self._config.get_env(workspace_id))        # global + workspace（workspace wins）
        if user_id is not None and self._user_config is not None:
            base_env.update(self._user_config.get_env(user_id, workspace_id))  # user×workspace 覆盖
        secret_env = dict(self._secrets.get_secrets(workspace_id))
        env = {**base_env, **secret_env}                            # secret 最高
        spec = self._specs.get(workspace_id, WorkspaceSpec())
        return ResolvedWorkspace(
            workspace_id=workspace_id,
            cwd=os.path.join(self._root, workspace_id),
            env=env,
            sensitive_keys=frozenset(secret_env.keys()),
            allowed_skills=spec.allowed_skills,
            backend_name=spec.backend_name,
            max_rounds=spec.max_rounds,
        )
