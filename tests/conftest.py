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

OpenBao fixtures (P9b):
  openbao_container, openbao_endpoint, openbao_token, openbao_bad_token, and
  seed_secret are added alongside the Postgres fixtures. Same UNCONDITIONAL
  definition pattern applies — testcontainers gating happens INSIDE the
  fixture body via importorskip.

  Two-sentinel design (§4.5):
  - _TEST_TOKEN_SENTINEL: the GOOD dev root token; transmitted in network-error tests
  - _TEST_BAD_TOKEN_SENTINEL: the BAD sentinel; transmitted in auth-error tests
  Both are shaped distinctly enough to grep without false positives.
  HC-D tests scan the sentinel that was ACTUALLY TRANSMITTED in the failing request.

MySQL fixtures (P9a.1):
  mysql_container and mysql_dsn_and_connect_args follow the same unconditional
  definition pattern as Postgres/OpenBao. The fixture returns a credential-free
  DSN plus a separate connect_args dict because HC-C rejects credentials embedded
  in URLs and pymysql has no PGUSER/PGPASSWORD-style fallback.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from testcontainers.mysql import MySqlContainer
    from testcontainers.postgres import PostgresContainer
    from testcontainers.vault import VaultContainer


# ---------------------------------------------------------------------------
# Configuration constants (read at module load — applied lazily inside fixtures)
# ---------------------------------------------------------------------------

# Pinned to a specific minor for full reproducibility (not `:16-alpine` floating tag).
# Bump explicitly in a follow-up PR; do NOT change to a floating tag.
_POSTGRES_IMAGE = "postgres:16.4-alpine"

# OpenBao image — pinned to 2.0.0 (verified published at implementation time).
# openbao/openbao:2.0.0 is the official release; bump explicitly if needed.
_OPENBAO_IMAGE = "openbao/openbao:2.0.0"

# MySQL image — pinned to 8.0.36 (P9a.1 locked minor).
_MYSQL_IMAGE = "mysql:8.0.36"
_MYSQL_TEST_USER = "claw_test"
_MYSQL_TEST_PASSWORD = "MYSQL-TEST-PW-DO-NOT-LEAK-AT-CONTAINER-LEVEL"
_MYSQL_TEST_DBNAME = "claw_test"

# Two sentinel tokens for OpenBao HC-D leakage tests.
# Shaped distinctly enough to grep across logs/CI output without false positives.
# The good sentinel is used as the dev root token (BAO_DEV_ROOT_TOKEN_ID).
# The bad sentinel is only used in HC-D auth-error tests (never transmitted on
# successful requests).
_TEST_TOKEN_SENTINEL = "OPENBAO-TEST-ROOT-DO-NOT-LEAK"
_TEST_BAD_TOKEN_SENTINEL = "OPENBAO-BAD-TOKEN-DO-NOT-LEAK"

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


# ---------------------------------------------------------------------------
# MySQL fixtures (P9a.1) — UNCONDITIONAL definitions; gating inside body
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mysql_container() -> Iterator["MySqlContainer"]:
    """Start a MySQL 8 container once per pytest session.

    Image: mysql:8.0.36 (pinned minor — see _MYSQL_IMAGE).

    Skip behaviour mirrors postgres_container:
      - testcontainers not installed → importorskip → clean Skipped
      - pymysql not installed → importorskip → clean Skipped
      - Docker daemon unreachable or container startup failure → pytest.skip
    """
    pytest.importorskip(
        "testcontainers",
        reason=(
            "testcontainers not installed — MySQL integration tests require Docker. "
            "Install with: pip install -e '.[persistence-mysql,integration]'"
        ),
    )
    pytest.importorskip(
        "pymysql",
        reason=(
            "pymysql not installed — MySQL integration tests require the MySQL "
            "adapter extra. Install with: pip install -e "
            "'.[persistence-mysql,integration]'"
        ),
    )

    from testcontainers.core.config import testcontainers_config as tc_config
    from testcontainers.mysql import MySqlContainer

    tc_config.max_tries = int(_TC_TIMEOUT / max(tc_config.sleep_time, 0.1))

    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host and "/var/run/docker.sock" not in docker_host:
        tc_config.ryuk_disabled = True

    try:
        container = MySqlContainer(
            image=_MYSQL_IMAGE,
            username=_MYSQL_TEST_USER,
            password=_MYSQL_TEST_PASSWORD,
            dbname=_MYSQL_TEST_DBNAME,
        )
        with container as c:
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker daemon not available or MySQL container failed: {exc}")


@pytest.fixture
def mysql_dsn_and_connect_args(
    mysql_container: "MySqlContainer",
) -> tuple[str, dict[str, Any]]:
    """Return credential-free MySQL DSN plus separate connect_args credentials."""
    host = mysql_container.get_container_host_ip()
    port = mysql_container.get_exposed_port(3306)
    dsn = f"mysql+pymysql://{host}:{port}/{_MYSQL_TEST_DBNAME}"
    connect_args = {
        "user": _MYSQL_TEST_USER,
        "password": _MYSQL_TEST_PASSWORD,
        "connect_timeout": 10,
    }
    return dsn, connect_args


# Silence unused-import warning when TYPE_CHECKING is False at runtime.
_ = Any


# ---------------------------------------------------------------------------
# OpenBao fixtures (P9b) — UNCONDITIONAL definitions; gating inside body
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def openbao_container() -> Iterator["VaultContainer"]:
    """Start an OpenBao container in dev mode once per pytest session.

    Image: openbao/openbao:2.0.0 (pinned minor — see _OPENBAO_IMAGE).
    Root token: _TEST_TOKEN_SENTINEL (set via BAO_DEV_ROOT_TOKEN_ID env var).
    Listen address: 0.0.0.0:8200 (set via BAO_DEV_LISTEN_ADDRESS env var).

    Container choice (§2.2 of P9b sub-plan):
      PRIMARY PATH — VaultContainer(image="openbao/openbao:2.0.0") with explicit
      BAO_DEV_* env vars. VaultContainer's __init__ sets VAULT_DEV_ROOT_TOKEN_ID
      via root_token kwarg (the HashiCorp Vault env var alias). We ALSO set
      BAO_DEV_ROOT_TOKEN_ID explicitly via .with_env() because OpenBao documents
      BAO_* as the canonical prefix and we must not rely on alias compatibility.
      VaultContainer's _healthcheck polls /v1/sys/health; OpenBao returns HTTP 200
      for an active/unsealed instance — same response shape as HashiCorp Vault,
      so the healthcheck succeeds. Primary path used.

      FALLBACK PATH (documented but not used) — if VaultContainer's healthcheck
      fails for OpenBao (e.g. response shape changes in a future version), fall
      back to DockerContainer with wait_for_logs("core: post-unseal setup complete").
      See §2.2 fallback code snippet in the sub-plan for the pattern.

    Skip behaviour (matters for the install matrix):
      - testcontainers not installed → importorskip → clean Skipped (not error)
      - Docker daemon unreachable → except Exception → pytest.skip with message
      - Container start fails → same path → clean skip

    Colima / Rancher Desktop socket note:
      Same ryuk_disabled logic as postgres_container — applied consistently.
    """
    pytest.importorskip(
        "testcontainers",
        reason=(
            "testcontainers not installed — OpenBao integration tests require Docker. "
            "Install with: pip install -e '.[secrets-openbao,integration]'"
        ),
    )

    from testcontainers.core.config import testcontainers_config as tc_config
    from testcontainers.vault import VaultContainer

    tc_config.max_tries = int(_TC_TIMEOUT / max(tc_config.sleep_time, 0.1))

    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host and "/var/run/docker.sock" not in docker_host:
        tc_config.ryuk_disabled = True

    try:
        # VaultContainer.__init__ sets VAULT_DEV_ROOT_TOKEN_ID via root_token kwarg.
        # We also set BAO_DEV_ROOT_TOKEN_ID explicitly — OpenBao's canonical env var.
        # Both are set; redundancy is intentional (never rely on alias compatibility).
        container = (
            VaultContainer(
                image=_OPENBAO_IMAGE,
                root_token=_TEST_TOKEN_SENTINEL,
            )
            .with_env("BAO_DEV_ROOT_TOKEN_ID", _TEST_TOKEN_SENTINEL)
            .with_env("BAO_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
        )
        # I1 fix: use context-manager protocol so container.stop() runs even if
        # a test using this fixture raises an exception that bubbles through yield.
        # Symmetric with postgres_container above. Bare yield + stop() would leak
        # the container (port 8200 held forever on Colima/Rancher where Ryuk is
        # disabled), causing subsequent runs to fail with port collisions.
        with container as c:
            yield c
    except Exception as exc:  # noqa: BLE001
        # Covers: docker.errors.DockerException, ConnectionError, healthcheck timeout,
        # and any testcontainers startup failure (image pull, network, etc.).
        pytest.skip(
            f"Docker daemon not available or OpenBao container failed to start: {exc}"
        )


@pytest.fixture
def openbao_endpoint(openbao_container: "VaultContainer") -> str:
    """Return the OpenBao base URL (e.g. 'http://localhost:54321').

    Credential-free — caller passes the token separately via openbao_token
    fixture (HC-C: credentials never embedded in URL).

    Automatic skip: inherits skip from openbao_container if Docker unavailable.
    """
    host = openbao_container.get_container_host_ip()
    port = openbao_container.get_exposed_port(8200)
    return f"http://{host}:{port}"


@pytest.fixture
def openbao_token() -> str:
    """Return the GOOD dev root token sentinel.

    This is the token transmitted by happy-path and network-error tests.
    HC-D network-error test scans for this sentinel — the token actually
    transmitted in the failing request.

    Note: openbao_container sets BAO_DEV_ROOT_TOKEN_ID to this same value,
    so this fixture is the correct credential for successful OpenBao calls.
    """
    return _TEST_TOKEN_SENTINEL


@pytest.fixture
def openbao_bad_token() -> str:
    """Return the BAD token sentinel for HC-D auth-error tests.

    This token is transmitted in the failing auth request (OpenBao returns
    401/403 because it's not a valid root token). HC-D auth-error test scans
    for this sentinel — the token actually transmitted in the failing request.

    The good root token (_TEST_TOKEN_SENTINEL) is NEVER transmitted on auth-
    error paths, so scanning for it there would be a false-green.
    """
    return _TEST_BAD_TOKEN_SENTINEL


@pytest.fixture
def seed_secret(
    openbao_endpoint: str,
    openbao_token: str,
) -> Callable[[str, dict[str, str]], None]:
    """Return a helper closure that seeds a KV v2 secret at the given path.

    The helper POSTs to /v1/secret/data/{path} using requests (no hvac,
    mirroring the P8d adapter decision — see sub-plan §1 rationale).

    Usage:
        seed_secret("claw/workspaces/ws-1", {"OPENAI_KEY": "sk-test"})

    Raises if the POST fails (HTTP error → requests.HTTPError via raise_for_status).
    Automatic skip: inherits skip from openbao_endpoint → openbao_container.
    """
    import requests as _requests

    def _seed(path: str, data: dict[str, str]) -> None:
        url = f"{openbao_endpoint}/v1/secret/data/{path}"
        resp = _requests.post(
            url,
            headers={"X-Vault-Token": openbao_token},
            json={"data": data},
            timeout=10,
        )
        resp.raise_for_status()

    return _seed
