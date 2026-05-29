import pytest
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
from tests.contract import sessionstore_contract as sc

# HC-D: Include sqlalchemy-sqlite in the contract suite if sqlalchemy is
# available (default CI).  When sqlalchemy is NOT installed, the memory +
# sqlite cases below must still run — so we conditionally extend the
# parametrize list rather than gating the whole module.
# mysql/postgres are gated behind @pytest.mark.integration in tests/integration/.
try:
    import claw_engine.adapters.persistence.sqlalchemy  # noqa: F401
    _SA_AVAILABLE = True
except ImportError:
    _SA_AVAILABLE = False


def _make_sa_store():
    from claw_engine.adapters.persistence.sqlalchemy import SQLAlchemySessionStore
    return SQLAlchemySessionStore("sqlite:///:memory:")


STORES = [
    ("memory", lambda: MemorySessionStore()),
    ("sqlite", lambda: SqliteSessionStore(":memory:")),
]

# HC-D: add sqlalchemy-sqlite to contract suite if available
if _SA_AVAILABLE:
    STORES.append(("sqlalchemy-sqlite", _make_sa_store))


@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_get_or_create_idempotent(name, make_store):
    sc.assert_get_or_create_idempotent(make_store)

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_different_key_different_session(name, make_store):
    sc.assert_different_key_different_session(make_store)

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_save_persists_turn(name, make_store):
    sc.assert_save_persists_turn(make_store)

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_dedup_tracks_message_ids(name, make_store):
    sc.assert_dedup_tracks_message_ids(make_store)
