"""Integration conftest — fixtures live in tests/conftest.py (top-level).

Fixture scoping decision (§14 #14 of P9a sub-plan):
  postgres_container, postgres_url, and unique_table_prefix are defined in
  tests/conftest.py (the top-level conftest) so they are available repo-wide —
  specifically in tests/contract/test_sessionstore_contract.py which also needs
  the postgres fixtures for the sqlalchemy-postgres parametrize case.

  We cannot use pytest_plugins here to re-export from a peer conftest (it would
  cause "Plugin already registered" errors because pytest auto-discovers all
  conftest.py files in its test path). The top-level conftest is the correct
  single source of truth for shared fixtures.

  This file exists as a documentation anchor and to hold integration-specific
  constants if needed in the future. The importorskip guard below ensures the
  integration test module itself fails gracefully if testcontainers is not
  installed.

Skip behaviour (documented here for co-location with integration tests):
  - If testcontainers is not installed: pytest.importorskip in
    test_sqlalchemy_session_store_integration.py skips that file cleanly.
  - If Docker daemon is unreachable: postgres_container fixture in tests/conftest.py
    calls pytest.skip("Docker daemon not available…") — graceful skip.

Default CI invocation:
    pytest -q -m "not integration"   ← no Docker needed; 519 + 1 skipped

Integration CI invocation:
    pytest -q -m integration tests/  ← requires Docker; 9 cases pass
    (note: `tests/` NOT `tests/integration/` — the postgres contract cases in
    tests/contract/test_sessionstore_contract.py would be missed otherwise)
"""
# No fixtures defined here — they live in tests/conftest.py.
# This file is intentionally minimal.
