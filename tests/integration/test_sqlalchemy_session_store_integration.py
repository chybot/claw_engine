"""Integration tests: SQLAlchemySessionStore against real SQL backends.

These tests use testcontainers to spin up real database containers. They are
expensive and require Docker, so they are gated with @pytest.mark.integration
and skipped in default CI.

Run with:
    pytest -q -m integration tests/
    (use `tests/` NOT `tests/integration/` — the SQLAlchemy contract cases in
    tests/contract/test_sessionstore_contract.py would be missed otherwise)

Prerequisites:
    pip install -e ".[persistence-postgres,persistence-mysql,integration]"
    Docker daemon running
"""
from __future__ import annotations

import threading
import uuid

import pytest

# Gate only imports this module uses directly. testcontainers is deliberately
# gated inside fixture bodies in tests/conftest.py so `pytest -m "not integration"`
# can deselect this file without producing a collection-time testcontainers skip.
pytest.importorskip(
    "sqlalchemy",
    reason=(
        "sqlalchemy not installed — integration tests require the SQLAlchemy adapter. "
        "Install with: pip install -e "
        "'.[persistence-postgres,persistence-mysql,integration]'"
    ),
)

# Safe to import the heavy stuff now (sqlalchemy gate passed).
import sqlalchemy  # noqa: E402

from claw_engine.adapters.persistence.sqlalchemy import SQLAlchemySessionStore  # noqa: E402


# ---------------------------------------------------------------------------
# MySQL tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_mysql_end_to_end_crud_with_connect_args_auth(
    mysql_dsn_and_connect_args: tuple[str, dict[str, object]],
    unique_table_prefix: str,
) -> None:
    """MySQL end-to-end SessionStore CRUD via credential-free DSN + connect_args."""
    dsn, connect_args = mysql_dsn_and_connect_args
    assert "@" not in dsn
    assert "test:test" not in dsn

    natural_key = dict(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )
    store = SQLAlchemySessionStore(
        dsn,
        table_prefix=unique_table_prefix,
        connect_args=connect_args,
    )

    session = store.get_or_create(**natural_key)
    assert session.session_id
    assert session.backend_thread_id is None
    assert session.round_count == 0

    again = store.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="other",
        max_rounds=10,
    )
    assert again.session_id == session.session_id
    assert again.backend_name == "codex"
    assert again.max_rounds == 50

    store.save(session.with_turn(backend_thread_id="mysql-thread-1", now=123.0))
    saved = store.get_or_create(**natural_key)
    assert saved.backend_thread_id == "mysql-thread-1"
    assert saved.round_count == 1
    assert saved.last_active == 123.0

    store.mark_processed(saved.session_id, "msg-1")
    assert store.is_processed(saved.session_id, "msg-1") is True
    store.mark_processed(saved.session_id, "msg-1")
    assert store.is_processed(saved.session_id, "msg-1") is True


@pytest.mark.integration
def test_mysql_fresh_schema_create_only(
    mysql_dsn_and_connect_args: tuple[str, dict[str, object]],
    unique_table_prefix: str,
) -> None:
    """Two stores against a fresh MySQL prefix initialize idempotently."""
    dsn, connect_args = mysql_dsn_and_connect_args

    store1 = SQLAlchemySessionStore(
        dsn,
        table_prefix=unique_table_prefix,
        connect_args=connect_args,
    )
    created = store1.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )
    assert created.session_id
    if store1._engine is not None:
        store1._engine.dispose()

    store2 = SQLAlchemySessionStore(
        dsn,
        table_prefix=unique_table_prefix,
        connect_args=connect_args,
    )
    reloaded = store2.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="other",
        max_rounds=10,
    )
    assert reloaded.session_id == created.session_id
    assert reloaded.backend_name == "codex"
    assert reloaded.max_rounds == 50


@pytest.mark.integration
def test_mysql_cross_restart_durability(
    mysql_dsn_and_connect_args: tuple[str, dict[str, object]],
    unique_table_prefix: str,
) -> None:
    """A fresh store instance sees MySQL state written by an earlier instance."""
    dsn, connect_args = mysql_dsn_and_connect_args
    natural_key = dict(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )

    store1 = SQLAlchemySessionStore(
        dsn,
        table_prefix=unique_table_prefix,
        connect_args=connect_args,
    )
    session = store1.get_or_create(**natural_key)
    saved_id = session.session_id
    store1.save(session.with_turn(backend_thread_id="backend-thread-1", now=456.0))
    store1.mark_processed(saved_id, "msg-1")
    if store1._engine is not None:
        store1._engine.dispose()
    del store1

    store2 = SQLAlchemySessionStore(
        dsn,
        table_prefix=unique_table_prefix,
        connect_args=connect_args,
    )
    rehydrated = store2.get_or_create(**natural_key)
    assert rehydrated.session_id == saved_id
    assert rehydrated.backend_thread_id == "backend-thread-1"
    assert rehydrated.round_count == 1
    assert rehydrated.last_active == 456.0
    assert store2.is_processed(saved_id, "msg-1") is True


# ---------------------------------------------------------------------------
# Postgres tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_postgres_fixture_url_passes_p8e_validation(
    postgres_url: str,
    unique_table_prefix: str,
) -> None:
    """The fixture URL must pass P8e validation — no embedded credentials, correct scheme.

    Specifically verifies:
    - HC-A: construction is hermetic (no IO in __init__)
    - HC-C: no credentials embedded in the DSN (would raise ValueError if present)
    - Actual DB connection works via env-var auth (PGUSER/PGPASSWORD)
    """
    # Construction must not raise (HC-A no IO, HC-C no creds)
    store = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    # First Protocol call exercises actual connection via env-var auth
    session = store.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )
    assert session.session_id  # actual auth + connection succeeded


@pytest.mark.integration
def test_concurrent_get_or_create_deterministically_triggers_integrity_error_on_postgres(
    postgres_url: str,
    unique_table_prefix: str,
) -> None:
    """P1-#2 killer test: two threads race INSERT; one gets IntegrityError; pool stays clean.

    Plain threading.Barrier is NOT sufficient: a fast first thread could complete
    INSERT before the second thread's SELECT, in which case the second thread sees
    the existing row and never tries INSERT — no IntegrityError, bug not exercised.

    Deterministic approach: register a SQLAlchemy 'before_cursor_execute' event
    that blocks both threads at a Barrier right BEFORE the INSERT statement (after
    each has done its SELECT). Both threads then proceed to INSERT simultaneously,
    forcing the second to hit UNIQUE-constraint violation.

    The event hook MUST be removed in finally BEFORE post-race assertions.
    Without event.remove, mark_processed / fresh get_or_create also issue
    INSERTs into the same table — only the main thread reaches the barrier,
    barrier.wait() times out, test deadlocks.
    """
    store = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)

    # PREWARM: trigger lazy _ensure_engine BEFORE attaching events, so the engine
    # exists and the event hook is registered on the right Engine instance.
    # Use a throwaway natural key so the test natural key has fresh state.
    store.get_or_create(
        workspace_id="prewarm",
        channel="x",
        external_thread_key="x",
        backend_name="codex",
        max_rounds=50,
    )

    insert_barrier = threading.Barrier(2, timeout=10)
    # Counter to prove the race actually occurred. If a future refactor renames
    # store._engine or otherwise breaks the hook registration, this counter would
    # stay at 0 — and the post-race assertion below would fail loudly rather
    # than silently green-lighting a degenerate test.
    fire_lock = threading.Lock()
    fire_count = [0]

    @sqlalchemy.event.listens_for(store._engine, "before_cursor_execute")
    def block_inserts_until_both_arrive(
        conn: object,
        cursor: object,
        statement: str,
        params: object,
        context: object,
        executemany: bool,
    ) -> None:
        # Block ONLY on the sessions-table INSERT, not on every statement.
        sessions_table = f"{unique_table_prefix}sessions"
        if (
            statement.lstrip().upper().startswith("INSERT INTO")
            and sessions_table in statement
        ):
            with fire_lock:
                fire_count[0] += 1
            insert_barrier.wait()

    natural_key = dict(
        workspace_id="ws-race",
        channel="slack",
        external_thread_key="thread-race",
        backend_name="codex",
        max_rounds=50,
    )

    results: list[object] = [None, None]
    errors: list[object] = [None, None]

    def racer(idx: int) -> None:
        try:
            results[idx] = store.get_or_create(**natural_key)
        except Exception as exc:  # noqa: BLE001
            errors[idx] = exc

    t1 = threading.Thread(target=racer, args=(0,))
    t2 = threading.Thread(target=racer, args=(1,))
    try:
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        # CRITICAL: remove the event listener BEFORE the post-race assertions
        # below. Otherwise mark_processed / fresh get_or_create also issue
        # INSERTs into the same table — only the main thread reaches the
        # barrier, barrier.wait() times out, test deadlocks.
        sqlalchemy.event.remove(
            store._engine, "before_cursor_execute", block_inserts_until_both_arrive
        )

    # KILLER-TEST SENTINEL: prove the deterministic race actually occurred.
    # If store._engine ever gets renamed/moved by a refactor, the @event hook
    # would silently register on the wrong object (or fail outright). Without
    # this assertion, threads serialise naturally, errors == [None, None]
    # passes, and the test goes silently green while the bug it guards against
    # is never exercised. fire_count >= 2 means both racers' INSERT statements
    # hit the hook (each thread's session-table INSERT increments once).
    assert fire_count[0] >= 2, (
        f"Race hook fired only {fire_count[0]} times — expected >= 2. "
        f"The deterministic race did NOT occur (both threads serialised or "
        f"the hook missed entirely). Check that store._engine refers to the "
        f"same Engine instance that get_or_create uses."
    )

    # Both threads completed without escaping exception
    assert errors == [None, None], f"unexpected exceptions: {errors}"

    # Both returned the same session_id (the winner's)
    assert results[0] is not None and results[1] is not None
    assert results[0].session_id == results[1].session_id  # type: ignore[union-attr]

    # CRITICAL P1-#2 ASSERTION: post-race connection pool must work.
    # If the race left a connection in aborted-transaction state, the next
    # write would fail with Postgres error "current transaction is aborted,
    # commands ignored until end of transaction block".
    # (Event listener already removed above, so these INSERTs don't hit barrier.)
    session = results[0]
    store.mark_processed(session.session_id, "msg-after-race")  # type: ignore[union-attr]
    assert store.is_processed(session.session_id, "msg-after-race") is True  # type: ignore[union-attr]

    # Also: a completely fresh get_or_create on different natural key
    fresh = store.get_or_create(
        workspace_id="ws-2",
        channel="slack",
        external_thread_key="thread-Y",
        backend_name="codex",
        max_rounds=50,
    )
    assert fresh.session_id != session.session_id  # type: ignore[union-attr]


@pytest.mark.integration
def test_duplicate_mark_processed_does_not_break_subsequent_ops(
    postgres_url: str,
    unique_table_prefix: str,
) -> None:
    """P1-#2 sibling: duplicate mark_processed must not leave conn in aborted state.

    Subsequent is_processed + mark_processed of different message must succeed.
    If P1-#2 regressed, the second mark_processed call would raise on Postgres
    with "current transaction is aborted, commands ignored until end of
    transaction block".
    """
    store = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    session = store.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )

    store.mark_processed(session.session_id, "msg-1")
    store.mark_processed(session.session_id, "msg-1")  # duplicate — triggers IntegrityError path

    # If P1-#2 regressed, the next line would raise on Postgres
    assert store.is_processed(session.session_id, "msg-1") is True
    store.mark_processed(session.session_id, "msg-2")  # fresh msg, must succeed
    assert store.is_processed(session.session_id, "msg-2") is True


@pytest.mark.integration
def test_cross_restart_session_persists(
    postgres_url: str,
    unique_table_prefix: str,
) -> None:
    """Container survives between two SQLAlchemySessionStore instances; data persists."""
    # Phase 1: create + save + dispose
    store1 = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    session = store1.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )
    saved_id = session.session_id
    updated = session.with_turn(backend_thread_id="backend-thread-1")
    store1.save(updated)
    store1.mark_processed(saved_id, "msg-1")
    # Drop store1 reference (engine GC'd)
    del store1

    # Phase 2: fresh store, same DB + same table_prefix → must see persisted state
    store2 = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    rehydrated = store2.get_or_create(
        workspace_id="ws",
        channel="slack",
        external_thread_key="t",
        backend_name="codex",
        max_rounds=50,
    )
    assert rehydrated.session_id == saved_id
    assert rehydrated.backend_thread_id == "backend-thread-1"
    assert rehydrated.round_count == 1
    assert store2.is_processed(saved_id, "msg-1") is True


@pytest.mark.integration
def test_postgres_schema_argument_creates_tables_in_schema(
    postgres_url: str,
    unique_table_prefix: str,
) -> None:
    """Verify P8e C1 fix on real Postgres: schema= actually puts tables in that schema."""
    schema_name = f"claw_test_{uuid.uuid4().hex[:6]}"

    # Single raw engine for both DDL and verification — explicit dispose() in
    # finally prevents pool leaks across repeated test runs.
    raw_engine = sqlalchemy.create_engine(postgres_url)
    try:
        # Create the schema first (testcontainers gives us 'test' user with CREATEDB;
        # CREATE SCHEMA is allowed).
        with raw_engine.begin() as conn:
            conn.execute(sqlalchemy.text(f"CREATE SCHEMA {schema_name}"))

        store = SQLAlchemySessionStore(
            postgres_url,
            schema=schema_name,
            table_prefix=unique_table_prefix,
        )
        session = store.get_or_create(
            workspace_id="ws",
            channel="slack",
            external_thread_key="t",
            backend_name="codex",
            max_rounds=50,
        )
        assert session.session_id  # creation succeeded

        # Verify the table actually lives in the named schema
        with raw_engine.begin() as conn:
            exists = conn.execute(
                sqlalchemy.text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables"
                    "  WHERE table_schema = :schema"
                    "  AND table_name = :table_name"
                    ")"
                ),
                {"schema": schema_name, "table_name": f"{unique_table_prefix}sessions"},
            ).scalar()
            assert exists is True
    finally:
        raw_engine.dispose()
