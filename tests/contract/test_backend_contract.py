import pytest
from tests.contract import backend_contract as bc
from tests.contract import codex_fixtures

FIXTURES = [codex_fixtures.FIXTURE]


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_success_contract(fx):
    bc.assert_success(fx)


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_nonzero_exit_is_backend_crash(fx):
    bc.assert_nonzero_exit_is_backend_crash(fx)


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_malformed_is_protocol(fx):
    bc.assert_malformed_is_protocol(fx)


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_timeout_is_error(fx):
    bc.assert_timeout_is_error(fx)


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_crash_is_backend_crash(fx):
    bc.assert_crash_is_backend_crash(fx)
