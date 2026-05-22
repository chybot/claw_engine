from __future__ import annotations
from typing import Mapping, Optional
from claw_engine.engine.identity.contracts import User, UnknownUser


class InMemoryIdentityProvider:
    """V1 内存身份 + 授权。真实后端(SSO/LDAP)在 adapters 实现同一 Protocol。"""

    def __init__(self, users: Optional[Mapping[str, User]] = None,
                 authorized: Optional[Mapping[str, tuple[str, ...]]] = None) -> None:
        self._users = dict(users or {})                      # key = raw_user_ref
        self._authorized = {k: tuple(v) for k, v in (authorized or {}).items()}  # key = user_id

    def resolve_user(self, raw_user_ref: str) -> User:
        try:
            return self._users[raw_user_ref]
        except KeyError:
            raise UnknownUser(raw_user_ref) from None

    def authorized_workspaces(self, user: User) -> tuple[str, ...]:
        return self._authorized.get(user.user_id, ())

    def can_access_workspace(self, user: User, workspace_id: str) -> bool:
        return workspace_id in self.authorized_workspaces(user)
