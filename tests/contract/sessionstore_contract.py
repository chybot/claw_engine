"""跨实现共享的 SessionStore 行为契约。memory / sqlite 用同一套。"""
from __future__ import annotations
from typing import Callable
from claw_engine.engine.persistence.contracts import SessionStore

MakeStore = Callable[[], SessionStore]

_KW = dict(workspace_id="w", channel="c", external_thread_key="t", backend_name="fake", max_rounds=50)


def assert_get_or_create_idempotent(make_store: MakeStore) -> None:
    store = make_store()
    s1 = store.get_or_create(**_KW)
    s2 = store.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                             backend_name="other", max_rounds=10)
    assert s1.session_id == s2.session_id
    assert s2.backend_name == "fake"   # existing wins
    assert s2.max_rounds == 50


def assert_different_key_different_session(make_store: MakeStore) -> None:
    store = make_store()
    a = store.get_or_create(**{**_KW, "external_thread_key": "t1"})
    b = store.get_or_create(**{**_KW, "external_thread_key": "t2"})
    assert a.session_id != b.session_id


def assert_save_persists_turn(make_store: MakeStore) -> None:
    store = make_store()
    s = store.get_or_create(**_KW)
    store.save(s.with_turn(backend_thread_id="bt_1", now=123.0))
    again = store.get_or_create(**_KW)
    assert again.backend_thread_id == "bt_1"
    assert again.round_count == 1
    assert again.last_active == 123.0


def assert_dedup_tracks_message_ids(make_store: MakeStore) -> None:
    store = make_store()
    s = store.get_or_create(**_KW)
    assert not store.is_processed(s.session_id, "m1")
    store.mark_processed(s.session_id, "m1")
    assert store.is_processed(s.session_id, "m1")
    assert not store.is_processed(s.session_id, "m2")
    store.mark_processed(s.session_id, "m1")  # 幂等，不报错
