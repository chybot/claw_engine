from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
from tests.contract.fake_backend import FakeBackend


def _service(db_path):
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    return ConversationService(Engine(reg), SqliteSessionStore(db_path))

_KW = dict(workspace_id="w", channel="c", external_thread_key="t", cwd="/tmp", env={}, backend_name="fake")

def test_multiturn_resume_persists_across_restart(tmp_path):
    db = str(tmp_path / "s.db")
    svc1 = _service(db)
    r1 = svc1.handle(text="hello", **_KW)
    assert r1.backend_thread_id == "fake-thread-1"

    # 新进程/新 store（同 db 文件）= 模拟重启；应续接同一 backend_thread_id
    svc2 = _service(db)
    store2 = SqliteSessionStore(db)
    _STORE_KW = dict(workspace_id="w", channel="c", external_thread_key="t",
                     backend_name="fake", max_rounds=50)
    session = store2.get_or_create(**_STORE_KW)
    assert session.backend_thread_id == "fake-thread-1"
    assert session.round_count == 1

    r2 = svc2.handle(text="again", **_KW)
    assert r2.backend_thread_id == "fake-thread-1"   # 续接，非新会话
    session2 = SqliteSessionStore(db).get_or_create(**_STORE_KW)
    assert session2.round_count == 2
