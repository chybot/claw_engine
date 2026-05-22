# claw_engine/engine/channels/runner.py
from __future__ import annotations
from typing import Mapping
from claw_engine.engine.channels.contracts import MessagingGateway, ReplyTarget, WorkspaceRouter
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.runtime.contracts import AgentRunResult


class ChannelRunner:
    """串通入站闭环：raw → verify → parse → route → WorkspaceConversationGateway → send_text。"""

    def __init__(self, gateway: MessagingGateway, router: WorkspaceRouter,
                 conversation: WorkspaceConversationGateway, *, default_backend_name: str) -> None:
        self._gateway = gateway
        self._router = router
        self._conversation = conversation
        self._default_backend = default_backend_name

    def handle_raw(self, raw: str, headers: Mapping[str, str]) -> AgentRunResult:
        self._gateway.verify_inbound(raw, headers)        # 失败抛 InboundAuthError，阻断后续
        msg = self._gateway.parse_inbound(raw)
        decision = self._router.route(msg)
        result = self._conversation.handle(
            workspace_id=decision.workspace_id, channel=msg.channel,
            external_thread_key=msg.external_thread_key, text=msg.text,
            backend_name=self._default_backend, message_id=msg.message_id,
            user_id=decision.user_id,
        )
        target = ReplyTarget(channel=msg.channel, external_thread_key=msg.external_thread_key,
                             raw_user_ref=msg.raw_user_ref)
        self._gateway.send_text(target, result.final_text)
        return result
