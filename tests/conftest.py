"""Top-level test conftest: shared fixtures available across all test sub-packages.

Fixture scoping decision (§14 #14 of P9a sub-plan):
  Integration fixtures (postgres_container, postgres_url, unique_table_prefix)
  are defined here at the top-level so that tests/contract/test_sessionstore_contract.py
  can request them via request.getfixturevalue() without any cross-package import
  plumbing.

  Alternatives considered:
  - pytest_plugins in tests/conftest.py pointing to tests/integration/conftest.py:
    rejected — pytest auto-discovers tests/integration/conftest.py as a conftest
    already, causing "Plugin already registered" errors on double-load.
  - pytest_plugins in tests/contract/conftest.py: would work but requires creating
    a new conftest just for one declaration, and the same double-load problem applies
    when running from tests/ root.
  - Duplicating fixtures: wrong (DRY violation, would drift).

  The chosen pattern (fixtures in tests/conftest.py) matches how tests/fixtures/*
  helpers are shared across the suite — top-level conftest is the conventional
  location for repo-wide shared fixtures.

  tests/integration/conftest.py only holds the pytest.importorskip guard and
  re-exports via import from here to avoid duplication. (See that file for details.)
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

# ---------------------------------------------------------------------------
# testcontainers guard
# ---------------------------------------------------------------------------
# pytest.importorskip at module level would skip the entire conftest (including
# non-integration tests). Instead we guard only the fixtures themselves — if
# testcontainers is missing, the fixtures simply don't exist and tests requesting
# them get a "fixture not found" error (which is fine; they'll also have
# pytest.importorskip at the test-module level).
#
# We use a try/except here rather than importorskip so that the baseline (non-
# integration) suite keeps running normally when testcontainers is not installed.
try:
    from testcontainers.postgres import PostgresContainer
    from testcontainers.core.config import testcontainers_config as _tc_config  # noqa: F401
    _TC_AVAILABLE = True
    # Colima / Rancher Desktop expose Docker via a non-standard socket path that
    # Ryuk (the testcontainers resource cleanup daemon) cannot mount. Disable Ryuk
    # when DOCKER_HOST points to a non-standard socket — Ryuk is optional (it just
    # cleans up orphaned containers; pytest teardown handles cleanup anyway via the
    # context-manager protocol in postgres_container). Standard Docker Desktop still
    # works with Ryuk enabled (default).
    _docker_host = os.environ.get("DOCKER_HOST", "")
    if _docker_host and "/var/run/docker.sock" not in _docker_host:
        _tc_config.ryuk_disabled = True
except ImportError:
    _TC_AVAILABLE = False

# Pinned to a specific minor for full reproducibility (not `:16-alpine` floating tag).
# Bump explicitly in a follow-up PR; do NOT change to a floating tag.
_POSTGRES_IMAGE = "postgres:16.4-alpine"

# Container startup timeout (seconds). Override via env for slower CI runners.
_TC_TIMEOUT = int(os.environ.get("CLAW_TC_TIMEOUT_SECONDS", "60"))


if _TC_AVAILABLE:
    @pytest.fixture(scope="session")
    def postgres_container() -> "Iterator[PostgresContainer]":
        """Start a Postgres container once per pytest session, reused across all tests.

        Image: postgres:16.4-alpine (pinned minor — see _POSTGRES_IMAGE).
        Startup timeout: 60s (override with CLAW_TC_TIMEOUT_SECONDS env var).

        Skips gracefully if Docker daemon is not available.
        """
        try:
            container = PostgresContainer(
                image=_POSTGRES_IMAGE,
                username="test",
                password="test",
                dbname="test",
            )
            with container as c:
                yield c
        except Exception as exc:  # noqa: BLE001
            # Covers: docker.errors.DockerException, requests.exceptions.ConnectionError,
            # and any testcontainers startup failure.
            pytest.skip(f"Docker daemon not available or container failed to start: {exc}")

    @pytest.fixture
    def postgres_url(
        postgres_container: "PostgresContainer",
        monkeypatch: pytest.MonkeyPatch,
    ) -> str:
        """Return a credential-free DSN and set PGUSER/PGPASSWORD env vars.

        The URL form is:
            postgresql+psycopg://{host}:{port}/{db}
        — no user:pass@ embedded (required by P8e HC-C which rejects DSNs with
        embedded credentials).

        psycopg/libpq picks up credentials from PGUSER + PGPASSWORD env vars at
        connect time (documented libpq env-var fallback). monkeypatch ensures the
        env vars are restored after each test (function scope).

        See sub-plan §1.1 for the full rationale; §3 for this fixture design.
        """
        host = postgres_container.get_container_host_ip()
        port = postgres_container.get_exposed_port(5432)
        db = postgres_container.dbname
        user = postgres_container.username
        password = postgres_container.password

        # Set env vars so libpq reads them (monkeypatch auto-restores after test).
        monkeypatch.setenv("PGUSER", user)
        monkeypatch.setenv("PGPASSWORD", password)

        # Construct credential-free DSN (no user:pass@); host + port + db only.
        # Do NOT use postgres_container.get_connection_url() — it returns
        # postgresql+psycopg2://user:pass@host:port/db which embeds credentials
        # and uses the wrong driver name.
        return f"postgresql+psycopg://{host}:{port}/{db}"

    @pytest.fixture
    def unique_table_prefix() -> str:
        """Generate a per-test table prefix ('test_<8 hex chars>_') for isolation.

        Each test gets its own prefix so tests are independent and can run in the
        same Postgres instance without state leaking between them.
        """
        return f"test_{uuid.uuid4().hex[:8]}_"
