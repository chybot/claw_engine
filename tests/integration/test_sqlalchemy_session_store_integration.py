"""P9a integration tests: SQLAlchemySessionStore against real Postgres via testcontainers.

These tests use testcontainers to spin up a real Postgres container. They are
expensive and require Docker, so they are gated with @pytest.mark.integration
and skipped in default CI.

MySQL tests are marked pytest.skip("-> P9a.1") — MySQL integration requires a
small adapter extension (connect_args) to work around the credential-free DSN
constraint. See sub-plan §1.1 for full rationale.

Run with:
    pytest -q -m integration tests/
    (use `tests/` NOT `tests/integration/` — the postgres contract cases in
    tests/contract/test_sessionstore_contract.py would be missed otherwise)

Prerequisites:
    pip install -e ".[persistence-postgres,integration]"
    Docker daemon running
"""
from __future__ import annotations

import threading
import uuid

import pytest
import sqlalchemy

# Gate: entire module skipped unless testcontainers is installed.
pytest.importorskip(
    "testcontainers",
    reason=(
        "testcontainers not installed — integration tests require Docker. "
        "Install with: pip install -e '.[persistence-postgres,integration]'"
    ),
)

from claw_engine.adapters.persistence.sqlalchemy import SQLAlchemySessionStore  # noqa: E402


# ---------------------------------------------------------------------------
# MySQL stubs — deferred to P9a.1
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_mysql_end_to_end_crud() -> None:
    """Deferred to P9a.1 (requires connect_args adapter extension for credential-free DSN)."""
    pytest.skip("-> P9a.1")


@pytest.mark.integration
def test_mysql_cross_restart_durability() -> None:
    """Deferred to P9a.1."""
    pytest.skip("-> P9a.1")


@pytest.mark.integration
def test_mysql_schema_migration_on_fresh_db() -> None:
    """Deferred to P9a.1."""
    pytest.skip("-> P9a.1")


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

    # Create the schema first (testcontainers gives us 'test' user with CREATEDB;
    # CREATE SCHEMA is allowed).
    with sqlalchemy.create_engine(postgres_url).begin() as conn:
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
    with sqlalchemy.create_engine(postgres_url).begin() as conn:
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
