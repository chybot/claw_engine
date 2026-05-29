"""P8e integration tests: SQLAlchemySessionStore against real mysql + postgres.

These tests use testcontainers to spin up real database containers. They are
expensive and require Docker, so they are gated with @pytest.mark.integration
and skipped in default CI.

Run with:
    pytest -m integration tests/integration/

Prerequisites:
    pip install testcontainers pymysql psycopg[binary]
    Docker daemon running

Deferred from the P8e initial PR because:
1. testcontainers is a heavy dependency not needed for CI correctness.
2. The SQLite tests in tests/adapters/test_sqlalchemy_session_store.py
   fully cover the adapter's SQL semantics (parameterized queries, schema
   migration, CRUD, idempotency) via the same Core API path.
3. These tests will validate that the fake (SQLite) tests match reality.

When adding these tests:
- pytest.importorskip("testcontainers") at module level
- pytest.importorskip("pymysql") for mysql tests
- pytest.importorskip("psycopg") for postgres tests
- 3 test cases per backend:
  1. End-to-end CRUD (create, save, mark_processed, is_processed)
  2. Cross-restart durability (stop adapter, restart, retrieve session)
  3. Schema migration on fresh container (starts empty, adapter creates tables)
"""
from __future__ import annotations

import pytest

# Gate: entire module skipped unless testcontainers is installed
pytest.importorskip(
    "testcontainers",
    reason=(
        "testcontainers not installed — integration tests require Docker. "
        "Install with: pip install testcontainers pymysql 'psycopg[binary]'"
    ),
)


@pytest.mark.integration
def test_mysql_end_to_end_crud() -> None:
    """End-to-end CRUD against real MySQL container.

    Deferred: implement when testcontainers is available in CI.
    """
    pytest.skip("MySQL integration test not yet implemented (deferred from P8e PR)")


@pytest.mark.integration
def test_mysql_cross_restart_durability() -> None:
    """Session survives adapter dispose + reconstruct against same MySQL.

    Deferred: implement when testcontainers is available in CI.
    """
    pytest.skip("MySQL integration test not yet implemented (deferred from P8e PR)")


@pytest.mark.integration
def test_mysql_schema_migration_on_fresh_db() -> None:
    """Adapter creates tables on empty MySQL container.

    Deferred: implement when testcontainers is available in CI.
    """
    pytest.skip("MySQL integration test not yet implemented (deferred from P8e PR)")


@pytest.mark.integration
def test_postgres_end_to_end_crud() -> None:
    """End-to-end CRUD against real PostgreSQL container.

    Deferred: implement when testcontainers is available in CI.
    """
    pytest.skip("PostgreSQL integration test not yet implemented (deferred from P8e PR)")


@pytest.mark.integration
def test_postgres_cross_restart_durability() -> None:
    """Session survives adapter dispose + reconstruct against same Postgres.

    Deferred: implement when testcontainers is available in CI.
    """
    pytest.skip("PostgreSQL integration test not yet implemented (deferred from P8e PR)")


@pytest.mark.integration
def test_postgres_schema_migration_on_fresh_db() -> None:
    """Adapter creates tables on empty PostgreSQL container.

    Deferred: implement when testcontainers is available in CI.
    """
    pytest.skip("PostgreSQL integration test not yet implemented (deferred from P8e PR)")
