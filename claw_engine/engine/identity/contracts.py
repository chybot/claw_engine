from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


class UnknownUser(KeyError):
    def __init__(self, raw_user_ref: str) -> None:
        super().__init__(raw_user_ref)
        self.raw_user_ref = raw_user_ref


class WorkspaceAccessDenied(PermissionError):
    def __init__(self, user_id: str, workspace_id: str) -> None:
        super().__init__(f"user {user_id} 无权访问 workspace {workspace_id}")
        self.user_id = user_id
        self.workspace_id = workspace_id


class WorkspaceRouteError(RuntimeError):
    """无法路由到 workspace（如用户无 default_workspace）。区别于越权 WorkspaceAccessDenied。"""


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str
    roles: tuple[str, ...] = ()
    default_workspace: Optional[str] = None


@runtime_checkable
class IdentityProvider(Protocol):
    def resolve_user(self, raw_user_ref: str) -> User: ...               # 未知抛 UnknownUser
    def authorized_workspaces(self, user: User) -> tuple[str, ...]: ...
    def can_access_workspace(self, user: User, workspace_id: str) -> bool: ...
    def can_use_skill(self, user: User, workspace_id: str, skill: str) -> bool: ...
