"""Integration conftest — fixtures live in tests/conftest.py (top-level).

Fixture scoping decision (§14 #14 of P9a sub-plan, extended by P9a.1/P9b):
  postgres_container, postgres_url, mysql_container, mysql_dsn_and_connect_args,
  OpenBao fixtures, and unique_table_prefix are defined in
  tests/conftest.py (the top-level conftest) so they are available repo-wide —
  specifically in tests/contract/test_sessionstore_contract.py which also needs
  the SQLAlchemy container fixtures for postgres/mysql parametrize cases.

  We cannot use pytest_plugins here to re-export from a peer conftest (it would
  cause "Plugin already registered" errors because pytest auto-discovers all
  conftest.py files in its test path). The top-level conftest is the correct
  single source of truth for shared fixtures.

  This file exists as a documentation anchor and to hold integration-specific
  constants if needed in the future. Do NOT gate testcontainers at module import
  time here; fixture bodies in tests/conftest.py own that skip behavior so
  marker deselection remains clean.

Skip behaviour (documented here for co-location with integration tests):
  - If testcontainers is not installed: container fixtures in tests/conftest.py
    call pytest.importorskip("testcontainers") — graceful skip only for selected
    integration tests that request those fixtures.
  - If Docker daemon is unreachable: container fixtures call
    pytest.skip("Docker daemon not available…") — graceful skip.

Default CI invocation:
    pytest -q -m "not integration"   ← no Docker needed; 526 passed + 23 deselected

Integration CI invocation:
    pytest -q -m integration tests/  ← requires Docker; 23 cases pass
    (note: `tests/` NOT `tests/integration/` — the SQLAlchemy contract cases in
    tests/contract/test_sessionstore_contract.py would be missed otherwise)
"""
# No fixtures defined here — they live in tests/conftest.py.
# This file is intentionally minimal.
