from __future__ import annotations
import hashlib
import hmac
import json
from typing import List, Mapping, Tuple
from claw_engine.engine.channels.contracts import (
    IncomingMessage, InboundAuthError, ProgressHandle, ProgressState, ReplyTarget,
)


class SeaTalkChannel:
    """SeaTalk 适配器：签名是 sha256(body + secret)（algo-bot 现状），不是 HMAC。
    payload/reply 都 hermetic（不起真 SeaTalk API）。"""

    name = "seatalk"

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.sent_texts: List[Tuple[ReplyTarget, str]] = []
        self.sent_attachments: List[Tuple[ReplyTarget, Tuple[str, ...]]] = []
        self.progress_started: List[Tuple[ReplyTarget, Tuple[str, ...], str]] = []
        self.progress_updates: List[Tuple[str, ProgressState]] = []

    def verify_inbound(self, raw: str, headers: Mapping[str, str]) -> None:
        lower = {k.lower(): v for k, v in headers.items()}
        sig = lower.get("signature")
        if not sig:
            raise InboundAuthError("missing Signature")
        expected = hashlib.sha256((raw + self._secret).encode()).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise InboundAuthError("signature mismatch")

    def parse_inbound(self, raw: str) -> IncomingMessage:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed SeaTalk body: {exc}") from exc
        # 显式拒绝非目标 event_type（防任意 SeaTalk event 凑出字段就进 engine）
        if payload.get("event_type") != "message_from_bot_subscriber":
            raise ValueError(f"unsupported SeaTalk event_type: {payload.get('event_type')!r}")
        try:
            event = payload["event"]
            message = event["message"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"SeaTalk payload 缺少 event/message: {exc}") from exc
        # V1 只处理 text 消息——非 text(image/file/...) 显式拒绝，不在此 path 静默吞
        if message.get("tag") != "text":
            raise ValueError(f"unsupported SeaTalk message tag: {message.get('tag')!r}")
        try:
            text = message["text"]["plain_text"]
            email = event["sender"]["email"]
            thread = event["thread_id"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"SeaTalk payload 缺少必需字段: {exc}") from exc
        return IncomingMessage(
            channel=self.name,
            raw_user_ref=str(email),
            external_thread_key=str(thread),
            text=str(text),
            message_id=event.get("message_id"),
            attachments=(),                          # V1 不下载附件
            is_command=str(text).startswith("/"),
        )

    def send_text(self, target: ReplyTarget, text: str) -> None:
        self.sent_texts.append((target, text))

    def send_attachments(self, target: ReplyTarget, files: Tuple[str, ...]) -> None:
        self.sent_attachments.append((target, tuple(files)))

    def start_progress(self, target: ReplyTarget, steps: Tuple[str, ...]) -> ProgressHandle:
        handle = f"st-prog-{target.external_thread_key}"
        self.progress_started.append((target, tuple(steps), handle))
        return handle

    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None:
        self.progress_updates.append((handle, state))
