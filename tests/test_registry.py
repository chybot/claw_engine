# tests/test_registry.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry, BackendNotRegistered
from tests.contract.fake_backend import FakeBackend

def test_register_and_resolve_backend():
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    backend = reg.resolve_backend("fake")
    assert backend.name == "fake"

def test_unknown_backend_raises():
    reg = EngineRegistry()
    with pytest.raises(BackendNotRegistered):
        reg.resolve_backend("nope")

def test_resolve_is_fresh_instance():
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    assert reg.resolve_backend("fake") is not reg.resolve_backend("fake")
