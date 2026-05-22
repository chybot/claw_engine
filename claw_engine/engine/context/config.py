from __future__ import annotations
from typing import Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class ConfigProvider(Protocol):
    def get_env(self, workspace_id: str) -> Mapping[str, str]:
        """该 workspace 生效的 env：global + workspace 合并，workspace 覆盖 global。"""
        ...


class LayeredConfigProvider:
    """V1 内存两层 env。真实后端(公司 SCC)在 adapters 实现同一 Protocol。"""

    def __init__(self, global_env: Optional[Mapping[str, str]] = None,
                 workspace_env: Optional[Mapping[str, Mapping[str, str]]] = None) -> None:
        self._global = dict(global_env or {})
        self._workspace = {k: dict(v) for k, v in (workspace_env or {}).items()}

    def get_env(self, workspace_id: str) -> Mapping[str, str]:
        merged = dict(self._global)
        merged.update(self._workspace.get(workspace_id, {}))   # workspace wins
        return merged
