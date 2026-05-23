from __future__ import annotations
from typing import Mapping, Optional
from claw_engine.engine.identity.contracts import User, UnknownUser


class InMemoryIdentityProvider:
    """V1 内存身份 + 授权。真实后端(SSO/LDAP)在 adapters 实现同一 Protocol。"""

    def __init__(self, users: Optional[Mapping[str, User]] = None,
                 authorized: Optional[Mapping[str, tuple[str, ...]]] = None,
                 denied_skills: Optional[Mapping[str, tuple[str, ...]]] = None,
                 denied_workflows: Optional[Mapping[str, tuple[str, ...]]] = None) -> None:
        self._users = dict(users or {})                      # key = raw_user_ref
        self._authorized = {k: tuple(v) for k, v in (authorized or {}).items()}  # key = user_id
        self._denied_skills = {k: set(v) for k, v in (denied_skills or {}).items()}  # key = user_id
        self._denied_workflows = {k: set(v) for k, v in (denied_workflows or {}).items()}  # key = user_id

    def resolve_user(self, raw_user_ref: str) -> User:
        try:
            return self._users[raw_user_ref]
        except KeyError:
            raise UnknownUser(raw_user_ref) from None

    def authorized_workspaces(self, user: User) -> tuple[str, ...]:
        return self._authorized.get(user.user_id, ())

    def can_access_workspace(self, user: User, workspace_id: str) -> bool:
        return workspace_id in self.authorized_workspaces(user)

    def can_use_skill(self, user: User, workspace_id: str, skill: str) -> bool:
        # default-allow：未被显式 deny 即允许（workspace_id 预留给 adapter 做 per-workspace）
        return skill not in self._denied_skills.get(user.user_id, set())

    def can_run_workflow(self, user: User, workspace_id: str, workflow: str) -> bool:
        # default-allow（mirror can_use_skill）；workspace_id 预留给 adapter 做 per-workspace
        return workflow not in self._denied_workflows.get(user.user_id, set())
