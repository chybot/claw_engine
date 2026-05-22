from __future__ import annotations
import hashlib
import hmac
import json
from typing import List, Mapping, Tuple
from claw_engine.engine.channels.contracts import (
    IncomingMessage, InboundAuthError, ProgressHandle, ProgressState, ReplyTarget,
)


class WebhookChannel:
    """最小 webhook 风格 channel：HMAC-SHA256 校验 + JSON 解析 + 回复捕获（hermetic，不起 HTTP server）。"""

    name = "webhook"

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.sent_texts: List[Tuple[ReplyTarget, str]] = []
        self.sent_attachments: List[Tuple[ReplyTarget, Tuple[str, ...]]] = []
        self.progress_started: List[Tuple[ReplyTarget, Tuple[str, ...], str]] = []
        self.progress_updates: List[Tuple[str, ProgressState]] = []

    def verify_inbound(self, raw: str, headers: Mapping[str, str]) -> None:
        lower = {k.lower(): v for k, v in headers.items()}   # HTTP header 名大小写不敏感
        sig = lower.get("x-signature")
        if not sig:
            raise InboundAuthError("missing X-Signature")
        expected = hmac.new(self._secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise InboundAuthError("signature mismatch")

    def parse_inbound(self, raw: str) -> IncomingMessage:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed webhook body: {exc}") from exc
        try:
            user = payload["user"]
            thread = payload["thread"]
            text = payload["text"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"webhook payload 缺少必需字段: {exc}") from exc
        raw_attachments = payload.get("attachments", []) or []
        if not isinstance(raw_attachments, (list, tuple)):   # 防字符串被 tuple 成字符序列
            raise ValueError(f"attachments 必须是 list/tuple，得到 {type(raw_attachments).__name__}")
        return IncomingMessage(
            channel=self.name, raw_user_ref=str(user), external_thread_key=str(thread),
            text=str(text), message_id=payload.get("message_id"),
            attachments=tuple(str(a) for a in raw_attachments),
            is_command=str(text).startswith("/"),
        )

    def send_text(self, target: ReplyTarget, text: str) -> None:
        self.sent_texts.append((target, text))

    def send_attachments(self, target: ReplyTarget, files: Tuple[str, ...]) -> None:
        self.sent_attachments.append((target, tuple(files)))

    def start_progress(self, target: ReplyTarget, steps: Tuple[str, ...]) -> ProgressHandle:
        handle = f"prog-{target.external_thread_key}"
        self.progress_started.append((target, tuple(steps), handle))
        return handle

    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None:
        self.progress_updates.append((handle, state))   # P6a 仅记录；P6d 真正渲染
