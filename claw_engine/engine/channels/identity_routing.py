# claw_engine/engine/channels/identity_routing.py
from __future__ import annotations
from claw_engine.engine.channels.contracts import IncomingMessage, RouteDecision
from claw_engine.engine.identity.contracts import (
    IdentityProvider, WorkspaceAccessDenied, WorkspaceRouteError,
)


class IdentityWorkspaceRouter:
    """解析 raw_user_ref -> User -> 校验 default_workspace 授权 -> 路由。替换 P6a 占位路由。"""

    def __init__(self, identity: IdentityProvider) -> None:
        self._identity = identity

    def route(self, message: IncomingMessage) -> RouteDecision:
        user = self._identity.resolve_user(message.raw_user_ref)   # 未知 -> UnknownUser
        ws = user.default_workspace
        if ws is None:
            raise WorkspaceRouteError(f"user {user.user_id} 无 default_workspace，无法路由")
        if not self._identity.can_access_workspace(user, ws):      # 有 default 但越权
            raise WorkspaceAccessDenied(user.user_id, ws)
        return RouteDecision(workspace_id=ws, user_id=user.user_id)
