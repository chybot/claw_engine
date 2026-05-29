"""SessionStore contract suite — parametrised across all backends.

Backend roster
--------------
| Backend            | Marker      | Docker? |
|--------------------|-------------|---------|
| memory             | (none)      | No      |
| sqlite             | (none)      | No      |
| sqlalchemy-sqlite  | (none)      | No      |
| sqlalchemy-postgres| integration | Yes     |

Fixture-aware parametrize (§4.4 of P9a sub-plan)
-------------------------------------------------
The existing STORES list + @pytest.mark.parametrize approach worked for backends
whose factories could be built at module-load time. Postgres needs fixture-resolved
values (postgres_url, unique_table_prefix) that are only available at test-run time.

We restructure the runner to use a `make_store` fixture that dispatches on
`request.param` and calls `request.getfixturevalue(...)` for Postgres fixtures.
The `make_store` fixture returns `Callable[[], SessionStore]` — matching the
helper's contract exactly.

Helper file (tests/contract/sessionstore_contract.py) is UNCHANGED.

Fixture scoping note: postgres_url and unique_table_prefix live in
tests/integration/conftest.py and are re-exported via tests/conftest.py
(pytest_plugins = ["tests.integration.conftest"]), making them available here.
"""
from __future__ import annotations

import pytest

from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
from tests.contract import sessionstore_contract as sc

# HC-D: Include sqlalchemy-sqlite in the contract suite if sqlalchemy is
# available (default CI). When sqlalchemy is NOT installed, the memory +
# sqlite cases below must still run.
try:
    from claw_engine.adapters.persistence.sqlalchemy import SQLAlchemySessionStore
    _SA_AVAILABLE = True
except ImportError:
    _SA_AVAILABLE = False


_BACKENDS = [
    "memory",
    "sqlite",
    pytest.param(
        "sqlalchemy-sqlite",
        marks=pytest.mark.skipif(
            not _SA_AVAILABLE,
            reason="sqlalchemy not installed",
        ),
    ),
    pytest.param(
        "sqlalchemy-postgres",
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(
                not _SA_AVAILABLE,
                reason="sqlalchemy not installed",
            ),
        ],
    ),
]


@pytest.fixture(params=_BACKENDS)
def make_store(request: pytest.FixtureRequest, tmp_path: object) -> object:
    """Return a Callable[[], SessionStore] factory matching the helper's contract.

    For postgres, postgres_url + unique_table_prefix fixtures from
    tests/integration/conftest.py are loaded only when the postgres case is active
    (via request.getfixturevalue — no fixture resolution overhead for other backends).

    For sqlalchemy-sqlite and sqlalchemy-postgres: a single store instance is
    captured and the closure returns the same instance on every call. This is
    intentional — within a single test, the helper's two make_store() calls
    (e.g. one before save and one after) must observe the same DB state.
    """
    backend = request.param

    if backend == "memory":
        return lambda: MemorySessionStore()

    if backend == "sqlite":
        return lambda: SqliteSessionStore(":memory:")

    if backend == "sqlalchemy-sqlite":
        # Capture one engine/store; return same instance on every factory call
        # so cross-call persistence is visible (HC-D assert_save_persists_turn).
        store = SQLAlchemySessionStore("sqlite:///:memory:")
        return lambda: store

    if backend == "sqlalchemy-postgres":
        # postgres_url and unique_table_prefix are function-scoped fixtures from
        # tests/integration/conftest.py (available here via tests/conftest.py
        # pytest_plugins re-export).
        url: str = request.getfixturevalue("postgres_url")
        prefix: str = request.getfixturevalue("unique_table_prefix")
        store = SQLAlchemySessionStore(url, table_prefix=prefix)
        return lambda: store

    raise ValueError(f"unknown backend {backend!r}")  # pragma: no cover


def test_get_or_create_idempotent(make_store: object) -> None:
    sc.assert_get_or_create_idempotent(make_store)  # type: ignore[arg-type]


def test_different_key_different_session(make_store: object) -> None:
    sc.assert_different_key_different_session(make_store)  # type: ignore[arg-type]


def test_save_persists_turn(make_store: object) -> None:
    sc.assert_save_persists_turn(make_store)  # type: ignore[arg-type]


def test_dedup_tracks_message_ids(make_store: object) -> None:
    sc.assert_dedup_tracks_message_ids(make_store)  # type: ignore[arg-type]
