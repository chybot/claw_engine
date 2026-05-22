from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, runtime_checkable


class InboundAuthError(Exception):
    """入站校验失败（签名/token）。未通过则不进引擎。"""


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    raw_user_ref: str                       # 渠道原始身份，待 P6b IdentityProvider 解析
    external_thread_key: str
    text: str
    message_id: Optional[str] = None
    attachments: tuple[str, ...] = ()
    is_command: bool = False


@dataclass(frozen=True)
class RouteDecision:
    workspace_id: str
    user_id: Optional[str] = None


@dataclass(frozen=True)
class ReplyTarget:
    channel: str
    external_thread_key: str
    raw_user_ref: str


@dataclass(frozen=True)
class ProgressState:
    message: str
    fraction: Optional[float] = None


# 进度句柄对引擎不透明（channel 自定义）
ProgressHandle = str


@runtime_checkable
class MessagingGateway(Protocol):
    def verify_inbound(self, raw: str, headers: Mapping[str, str]) -> None: ...  # 失败抛 InboundAuthError
    def parse_inbound(self, raw: str) -> IncomingMessage: ...
    def send_text(self, target: ReplyTarget, text: str) -> None: ...
    def send_attachments(self, target: ReplyTarget, files: tuple[str, ...]) -> None: ...
    def start_progress(self, target: ReplyTarget, steps: tuple[str, ...]) -> ProgressHandle: ...
    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None: ...


@runtime_checkable
class WorkspaceRouter(Protocol):
    def route(self, message: IncomingMessage) -> "RouteDecision":
        """IncomingMessage -> 路由决策(workspace_id + 可选 user_id)。"""
        ...
