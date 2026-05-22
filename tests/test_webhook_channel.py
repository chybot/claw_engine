# tests/test_webhook_channel.py
import hashlib
import hmac
import json
import pytest
from claw_engine.engine.channels.contracts import InboundAuthError, ReplyTarget, ProgressState
from claw_engine.adapters.channels.webhook import WebhookChannel

SECRET = "s3cr3t"

def _sig(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()

def test_verify_inbound_accepts_valid_signature():
    ch = WebhookChannel(SECRET)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "hi"})
    ch.verify_inbound(body, {"X-Signature": _sig(body)})   # 不抛即通过
    ch.verify_inbound(body, {"x-signature": _sig(body)})   # header 名大小写不敏感

def test_verify_inbound_rejects_missing_and_wrong_signature():
    ch = WebhookChannel(SECRET)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "hi"})
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {})                         # 缺签名
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {"X-Signature": "deadbeef"})  # 错签名

def test_parse_inbound_maps_fields_and_command_flag():
    ch = WebhookChannel(SECRET)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "/help",
                       "message_id": "m1", "attachments": ["/tmp/a.png"]})
    msg = ch.parse_inbound(body)
    assert msg.channel == "webhook"
    assert msg.raw_user_ref == "u1" and msg.external_thread_key == "t1"
    assert msg.text == "/help" and msg.message_id == "m1"
    assert msg.attachments == ("/tmp/a.png",)
    assert msg.is_command is True
    # 非 slash 文本
    msg2 = ch.parse_inbound(json.dumps({"user": "u1", "thread": "t1", "text": "hi"}))
    assert msg2.is_command is False

def test_parse_inbound_malformed_raises_value_error():
    ch = WebhookChannel(SECRET)
    with pytest.raises(ValueError):
        ch.parse_inbound(json.dumps({"user": "u1"}))        # 缺 thread/text

def test_parse_inbound_rejects_non_list_attachments():
    ch = WebhookChannel(SECRET)
    with pytest.raises(ValueError):
        ch.parse_inbound(json.dumps({"user": "u", "thread": "t", "text": "hi",
                                     "attachments": "bad"}))   # 字符串非法，防被 tuple 成字符序列

def test_send_text_and_attachments_captured():
    ch = WebhookChannel(SECRET)
    target = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    ch.send_text(target, "hello")
    ch.send_attachments(target, ("/tmp/a.png",))
    assert ch.sent_texts == [(target, "hello")]
    assert ch.sent_attachments == [(target, ("/tmp/a.png",))]

def test_progress_is_recorded_and_returns_handle():
    ch = WebhookChannel(SECRET)
    target = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    handle = ch.start_progress(target, ("step1", "step2"))
    assert isinstance(handle, str)
    state = ProgressState(message="working", fraction=0.5)
    ch.update_progress(handle, state)
    assert ch.progress_started == [(target, ("step1", "step2"), handle)]   # 记录 start
    assert ch.progress_updates == [(handle, state)]                        # 记录 update
