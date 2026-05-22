# claw_engine/engine/channels/routing.py
from __future__ import annotations
from claw_engine.engine.channels.contracts import IncomingMessage, RouteDecision


class StaticWorkspaceRouter:
    """P6a 占位路由：任何消息固定路由到一个 workspace。P6b 用 IdentityProvider-based 路由替换。"""

    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id

    def route(self, message: IncomingMessage) -> RouteDecision:
        return RouteDecision(workspace_id=self._workspace_id, user_id=None)
