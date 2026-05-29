import hashlib
import json
import pytest
from claw_engine.adapters.channels.seatalk import SeaTalkChannel
from claw_engine.engine.channels.contracts import InboundAuthError, ProgressState, ReplyTarget

SECRET = "seatalk-shared-secret"

def _sig(body: str) -> str:
    return hashlib.sha256((body + SECRET).encode()).hexdigest()

def _payload(text="hello", email="alice@shopee.com", thread="thread-abc", msg_id="msg-1"):
    return json.dumps({
        "event_type": "message_from_bot_subscriber",
        "event": {
            "message": {"text": {"plain_text": text}, "tag": "text"},
            "sender": {"email": email},
            "thread_id": thread,
            "message_id": msg_id,
        },
    })

def test_verify_inbound_accepts_valid_sha256_signature():
    ch = SeaTalkChannel(SECRET)
    body = _payload()
    ch.verify_inbound(body, {"Signature": _sig(body)})
    ch.verify_inbound(body, {"signature": _sig(body)})       # 大小写不敏感

def test_verify_inbound_rejects_missing_and_wrong_signature():
    ch = SeaTalkChannel(SECRET)
    body = _payload()
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {})
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {"Signature": "deadbeef"})

def test_parse_inbound_maps_seatalk_event_to_incoming_message():
    ch = SeaTalkChannel(SECRET)
    msg = ch.parse_inbound(_payload(text="hello world"))
    assert msg.channel == "seatalk"
    assert msg.raw_user_ref == "alice@shopee.com"
    assert msg.external_thread_key == "thread-abc"
    assert msg.text == "hello world"
    assert msg.message_id == "msg-1"
    assert msg.is_command is False
    assert msg.attachments == ()

def test_parse_inbound_detects_slash_command():
    ch = SeaTalkChannel(SECRET)
    assert ch.parse_inbound(_payload(text="/info")).is_command is True

def test_parse_inbound_malformed_raises_value_error():
    ch = SeaTalkChannel(SECRET)
    with pytest.raises(ValueError):
        ch.parse_inbound("not json {{{")
    with pytest.raises(ValueError):
        # 正确 event_type 但缺 event 内层结构
        ch.parse_inbound(json.dumps({"event_type": "message_from_bot_subscriber"}))

def test_parse_inbound_rejects_non_target_event_type():
    """非目标 SeaTalk event 不应进 engine（防止任意 event 凑字段绕过）。"""
    ch = SeaTalkChannel(SECRET)
    bad = json.dumps({
        "event_type": "bot_added_to_group_chat",   # 任何非目标 event_type
        "event": {
            "message": {"text": {"plain_text": "hi"}, "tag": "text"},
            "sender": {"email": "a@b"}, "thread_id": "t", "message_id": "m",
        },
    })
    with pytest.raises(ValueError, match="event_type"):
        ch.parse_inbound(bad)

def test_parse_inbound_rejects_non_text_message_tag():
    """V1 只处理 text；image/file 等显式拒绝（不在此路径静默处理）。"""
    ch = SeaTalkChannel(SECRET)
    bad = json.dumps({
        "event_type": "message_from_bot_subscriber",
        "event": {
            "message": {"tag": "image", "image": {"url": "x"}},
            "sender": {"email": "a@b"}, "thread_id": "t", "message_id": "m",
        },
    })
    with pytest.raises(ValueError, match="tag"):
        ch.parse_inbound(bad)

def test_signature_with_non_ascii_body_locks_byte_semantics():
    """signature 计算与 parse 使用同一 raw str；non-ASCII body 不应破坏一致性。"""
    ch = SeaTalkChannel(SECRET)
    body = _payload(text="你好世界")
    ch.verify_inbound(body, {"Signature": _sig(body)})   # 不抛即通过
    msg = ch.parse_inbound(body)
    assert msg.text == "你好世界"
    # 用错的 raw（不同字符）算签名 → 必拒
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {"Signature": _sig(_payload(text="不同的"))})

def test_send_text_and_progress_captured():
    ch = SeaTalkChannel(SECRET)
    target = ReplyTarget(channel="seatalk", external_thread_key="t", raw_user_ref="a@b")
    ch.send_text(target, "reply text")
    h = ch.start_progress(target, ("step1",))
    ch.update_progress(h, ProgressState(message="ok"))
    assert ch.sent_texts == [(target, "reply text")]
    assert len(ch.progress_started) == 1 and len(ch.progress_updates) == 1
