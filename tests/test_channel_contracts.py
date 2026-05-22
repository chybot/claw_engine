import dataclasses
import pytest
from claw_engine.engine.channels.contracts import (
    IncomingMessage, ReplyTarget, ProgressState, InboundAuthError,
)

def test_incoming_message_frozen_and_defaults():
    m = IncomingMessage(channel="webhook", raw_user_ref="u1",
                        external_thread_key="t1", text="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.text = "x"
    assert m.message_id is None
    assert m.attachments == ()
    assert m.is_command is False

def test_reply_target_frozen_eq():
    a = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    b = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    assert a == b

def test_progress_state_defaults():
    s = ProgressState(message="working")
    assert s.fraction is None

def test_inbound_auth_error_is_exception():
    assert issubclass(InboundAuthError, Exception)
