# claw_engine/engine/context/user_config.py
from __future__ import annotations
from typing import Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class UserConfigProvider(Protocol):
    def get_env(self, user_id: str, workspace_id: str) -> Mapping[str, str]: ...


class InMemoryUserConfigProvider:
    """user_env: user_id -> workspace_id -> env。按 (user, workspace) 维度，避免跨 workspace 污染。"""

    def __init__(self,
                 user_env: Optional[Mapping[str, Mapping[str, Mapping[str, str]]]] = None) -> None:
        self._user_env = {
            uid: {wid: dict(env) for wid, env in by_ws.items()}
            for uid, by_ws in (user_env or {}).items()
        }

    def get_env(self, user_id: str, workspace_id: str) -> Mapping[str, str]:
        return dict(self._user_env.get(user_id, {}).get(workspace_id, {}))
