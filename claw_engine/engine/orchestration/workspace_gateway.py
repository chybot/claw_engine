# claw_engine/engine/orchestration/workspace_gateway.py
from __future__ import annotations
from typing import Optional
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.orchestration.conversation import ConversationService, DEFAULT_MAX_ROUNDS
from claw_engine.engine.runtime.contracts import AgentRunResult


class WorkspaceConversationGateway:
    """外层：workspace_id -> 解析 cwd/env/backend -> 交给 ConversationService（其签名不变）。"""

    def __init__(self, resolver: WorkspaceResolver, conversation: ConversationService) -> None:
        self._resolver = resolver
        self._conversation = conversation

    def handle(self, *, workspace_id: str, channel: str, external_thread_key: str,
               text: str, backend_name: str, message_id: Optional[str] = None,
               model: Optional[str] = None, user_id: Optional[str] = None) -> AgentRunResult:
        ws = self._resolver.resolve(workspace_id, user_id)
        return self._conversation.handle(
            workspace_id=workspace_id, channel=channel, external_thread_key=external_thread_key,
            text=text, cwd=ws.cwd, env=ws.env,
            backend_name=ws.backend_name or backend_name,   # workspace 指定的 backend 优先
            max_rounds=ws.max_rounds or DEFAULT_MAX_ROUNDS,
            message_id=message_id, model=model,
        )
