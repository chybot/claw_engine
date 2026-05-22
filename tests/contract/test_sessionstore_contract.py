import pytest
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from tests.contract import sessionstore_contract as sc

STORES = [("memory", lambda: MemorySessionStore())]

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
