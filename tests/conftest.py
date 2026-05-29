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

Install-matrix robustness (P9a code-review round 2):
  Fixtures are defined UNCONDITIONALLY. Optional-dependency gating happens
  INSIDE the fixture body via pytest.importorskip — when testcontainers is
  missing, importorskip raises Skipped and pytest converts that into a clean
  skip for any test requesting the fixture (not a "fixture not found" error).
  This matters for the partial-install case: sqlalchemy installed but
  testcontainers missing — the sqlalchemy-postgres parametrize case in
  tests/contract/test_sessionstore_contract.py must skip cleanly, not error.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from testcontainers.postgres import PostgresContainer


# ---------------------------------------------------------------------------
# Configuration constants (read at module load — applied lazily inside fixtures)
# ---------------------------------------------------------------------------

# Pinned to a specific minor for full reproducibility (not `:16-alpine` floating tag).
# Bump explicitly in a follow-up PR; do NOT change to a floating tag.
_POSTGRES_IMAGE = "postgres:16.4-alpine"

# Container startup timeout in seconds. Override via env for slower CI runners.
# Wired into testcontainers_config.timeout inside postgres_container().
_TC_TIMEOUT = int(os.environ.get("CLAW_TC_TIMEOUT_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Fixtures (UNCONDITIONAL — gating happens inside the body via importorskip)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> Iterator["PostgresContainer"]:
    """Start a Postgres container once per pytest session, reused across all tests.

    Image: postgres:16.4-alpine (pinned minor — see _POSTGRES_IMAGE).
    Startup timeout: 60s (override with CLAW_TC_TIMEOUT_SECONDS env var).

    Skip behaviour (matters for the install matrix):
      - testcontainers not installed → importorskip → clean Skipped (not error)
      - Docker daemon unreachable → except Exception → clean pytest.skip
      - Container start succeeds but image pull fails → same path → clean skip

    The driver="psycopg" kwarg is CRITICAL: testcontainers' PostgresContainer
    defaults driver="psycopg2", but pyproject installs psycopg[binary] (v3) via
    [persistence-postgres]. Without driver="psycopg", the readiness probe tries
    `import psycopg2`, ImportErrors, the exception bubbles up here, gets caught
    by our except, and converts to pytest.skip("Docker daemon not available…")
    — a silent false-green where Postgres tests never actually run. See P9a PR
    code review P1-#3 for the original observation.
    """
    # Optional-dependency gate INSIDE the fixture (not at module top) so that
    # this conftest stays importable in a clean .[dev] install where
    # testcontainers is absent. Any test requesting this fixture then gets a
    # clean skip with this reason.
    pytest.importorskip(
        "testcontainers",
        reason=(
            "testcontainers not installed — integration tests require Docker. "
            "Install with: pip install -e '.[persistence-postgres,integration]'"
        ),
    )

    from testcontainers.core.config import testcontainers_config as tc_config
    from testcontainers.postgres import PostgresContainer

    # Wire CLAW_TC_TIMEOUT_SECONDS to testcontainers' wait-loop timeout.
    # testcontainers computes effective timeout as `max_tries * sleep_time`
    # (sleep_time defaults to 1.0s). `timeout` itself is a read-only derived
    # property — we set max_tries to achieve the requested timeout in seconds.
    tc_config.max_tries = int(_TC_TIMEOUT / max(tc_config.sleep_time, 0.1))

    # Colima / Rancher Desktop expose Docker via a non-standard socket path
    # that Ryuk (the testcontainers resource cleanup daemon) cannot mount.
    # Disable Ryuk when DOCKER_HOST points to a non-standard socket — Ryuk is
    # optional (it just cleans up orphaned containers; pytest teardown handles
    # cleanup via the context-manager protocol below). Standard Docker Desktop
    # still works with Ryuk enabled (default).
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host and "/var/run/docker.sock" not in docker_host:
        tc_config.ryuk_disabled = True

    try:
        container = PostgresContainer(
            image=_POSTGRES_IMAGE,
            username="test",
            password="test",
            dbname="test",
            # P1-#3: use psycopg v3 driver to match what [persistence-postgres]
            # installs. Without this, the readiness probe ImportErrors on
            # psycopg2 and tests silently skip.
            driver="psycopg",
        )
        with container as c:
            yield c
    except Exception as exc:  # noqa: BLE001
        # Covers: docker.errors.DockerException, requests.exceptions.ConnectionError,
        # and any testcontainers startup failure (image pull, network, etc.).
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

    Automatic skip: if postgres_container skipped (testcontainers missing or
    Docker unreachable), this fixture inherits the skip — tests get a clean
    skip, not a fixture-resolution error.

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
    # Do NOT use postgres_container.get_connection_url() — it returns a URL
    # with embedded user:pass@ which would fail P8e HC-C validation.
    return f"postgresql+psycopg://{host}:{port}/{db}"


@pytest.fixture
def unique_table_prefix() -> str:
    """Generate a per-test table prefix ('test_<8 hex chars>_') for isolation.

    Each test gets its own prefix so tests are independent and can run in the
    same Postgres instance without state leaking between them.

    No testcontainers dependency — safe to use independent of Docker.
    """
    return f"test_{uuid.uuid4().hex[:8]}_"


# Silence unused-import warning when TYPE_CHECKING is False at runtime.
_ = Any
