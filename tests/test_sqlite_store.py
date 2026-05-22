from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore


def test_sqlite_persists_across_connections(tmp_path):
    db = str(tmp_path / "sessions.db")
    store1 = SqliteSessionStore(db)
    s = store1.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                             backend_name="fake", max_rounds=50)
    store1.save(s.with_turn(backend_thread_id="bt_9", now=42.0))
    store1.mark_processed(s.session_id, "m1")

    # 新连接（模拟重启）应看到已持久化的会话与去重记录
    store2 = SqliteSessionStore(db)
    again = store2.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                                 backend_name="ignored", max_rounds=1)
    assert again.session_id == s.session_id
    assert again.backend_thread_id == "bt_9"
    assert again.round_count == 1
    assert again.backend_name == "fake"   # 既有会话不被覆盖
    assert store2.is_processed(s.session_id, "m1")


def test_sqlite_uses_parameterized_queries():
    # 含特殊字符的输入不应破坏 SQL（参数化保证）
    store = SqliteSessionStore(":memory:")
    s = store.get_or_create(workspace_id="w'; DROP TABLE session; --", channel="c",
                            external_thread_key="t", backend_name="fake", max_rounds=50)
    assert s.session_id
    again = store.get_or_create(workspace_id="w'; DROP TABLE session; --", channel="c",
                                external_thread_key="t", backend_name="fake", max_rounds=50)
    assert again.session_id == s.session_id   # 表仍在、查询正常
