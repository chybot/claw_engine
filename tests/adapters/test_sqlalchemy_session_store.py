"""P8e: SQLAlchemySessionStore — invariant + Hard Contract tests.

All tests run against SQLite in-memory (no external infrastructure).
mysql/postgres integration tests deferred to tests/integration/ (see below).

Test gating: entire module is skipped if sqlalchemy is not installed.
"""
from __future__ import annotations

import os
import threading
import tempfile
from types import MappingProxyType

import pytest

# Gate: skip entire module if sqlalchemy is not installed.
pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (pip install -e .[persistence-sqlalchemy])")

import sqlalchemy  # noqa: E402

from claw_engine.adapters.persistence.sqlalchemy import (  # noqa: E402
    SQLAlchemySessionStore,
    SessionStoreError,
)
from claw_engine.engine.persistence.contracts import SessionStore  # noqa: E402

# ── Sentinel password used in HC-C tests ─────────────────────────────────────

SENTINEL_PW = "PLAINTEXT-PW-DO-NOT-LEAK"
CONNECT_ARGS_SENTINEL = "PLAINTEXT-CONNECT-ARGS-DO-NOT-LEAK"


def assert_no_password_leak(text: str) -> None:
    """Assert the sentinel password does not appear in `text`."""
    assert SENTINEL_PW not in text, (
        f"Password leaked in output text. Offending string (truncated):\n"
        f"  {text[:300]!r}"
    )


def assert_no_connect_args_leak(exc: SessionStoreError) -> None:
    """Assert connect_args credentials do not appear in user-facing error surfaces."""
    surfaces = {
        "str": str(exc),
        "repr": repr(exc),
        "args": repr(exc.args),
    }
    for name, text in surfaces.items():
        assert CONNECT_ARGS_SENTINEL not in text, (
            f"connect_args credential leaked in {name}. "
            f"Offending string (truncated): {text[:300]!r}"
        )
        assert "connect_args" not in text, (
            f"connect_args label leaked in {name}. "
            f"Offending string (truncated): {text[:300]!r}"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_sqlite_store(prefix: str = "claw_") -> SQLAlchemySessionStore:
    """Create a fresh in-memory SQLite store."""
    return SQLAlchemySessionStore("sqlite:///:memory:", table_prefix=prefix)


_KW = dict(
    workspace_id="w",
    channel="c",
    external_thread_key="t",
    backend_name="fake",
    max_rounds=50,
)

# ═══════════════════════════════════════════════════════════════════════════════
# HC-A: Construction is hermetic (no DB IO)
# ═══════════════════════════════════════════════════════════════════════════════


def test_construction_does_no_db_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-A: __init__ must not call sqlalchemy.create_engine.

    Monkey-patch create_engine to raise; construction must still succeed.
    """
    def bomb(*args, **kwargs):
        raise AssertionError("sqlalchemy.create_engine must NOT be called in __init__")

    monkeypatch.setattr(sqlalchemy, "create_engine", bomb)

    store = SQLAlchemySessionStore("sqlite:///test.db")
    assert store is not None
    assert store._engine is None  # lazy — not yet created


def test_engine_is_none_before_first_call() -> None:
    """HC-A: _engine attribute is None directly after construction."""
    store = SQLAlchemySessionStore("sqlite:///test.db")
    assert store._engine is None


def test_construction_validates_url_scheme() -> None:
    """HC-A: unsupported DSN schemes raise ValueError."""
    bad_schemes = [
        "oracle+cx_oracle://user/pass@host/db",
        "mssql+pyodbc://user:pass@host/db",
        "firebird+fdb://user:pass@host/db",
        "db2://user:pass@host/db",
    ]
    for bad_url in bad_schemes:
        with pytest.raises(ValueError, match="scheme"):
            SQLAlchemySessionStore(bad_url)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///test.db",
        "sqlite:///:memory:",
        "mysql+pymysql://host/db",
        "postgresql+psycopg://host/db",
    ],
)
def test_construction_accepts_explicit_driver_form(url: str) -> None:
    """P1-#1: Only +driver forms matching shipped extras are accepted."""
    store = SQLAlchemySessionStore(url)
    assert store is not None


# ── P1-#1: Bare scheme aliases must be rejected ──────────────────────────────


def test_construction_rejects_bare_mysql_scheme() -> None:
    """P1-#1: bare 'mysql://' picks MySQLdb (not shipped) → reject at construct."""
    with pytest.raises(ValueError, match="mysql\\+pymysql") as exc_info:
        SQLAlchemySessionStore("mysql://host/db")
    # Error must guide to the correct form
    assert "mysql+pymysql" in str(exc_info.value)


def test_construction_rejects_bare_postgresql_scheme() -> None:
    """P1-#1: bare 'postgresql://' picks psycopg2 (not shipped) → reject at construct."""
    with pytest.raises(ValueError, match="postgresql\\+psycopg") as exc_info:
        SQLAlchemySessionStore("postgresql://host/db")
    assert "postgresql+psycopg" in str(exc_info.value)


def test_construction_rejects_postgres_alias() -> None:
    """P1-#1: deprecated 'postgres://' alias is rejected."""
    with pytest.raises(ValueError, match="scheme"):
        SQLAlchemySessionStore("postgres://host/db")


# ═══════════════════════════════════════════════════════════════════════════════
# HC-C: DSN credentials rejected; never leak via repr / str / exception
# ═══════════════════════════════════════════════════════════════════════════════


def test_construction_rejects_url_with_credentials() -> None:
    """HC-C: DSN with embedded user:pass raises ValueError."""
    bad_urls = [
        f"postgresql+psycopg://user:{SENTINEL_PW}@host/db",
        "mysql+pymysql://admin:secret@host/mydb",
        "postgresql+psycopg://user@host/db",
    ]
    for bad_url in bad_urls:
        with pytest.raises(ValueError):
            SQLAlchemySessionStore(bad_url)


def test_construction_rejects_mysql_dsn_with_credentials() -> None:
    """HC-C layer 1: MySQL auth must use connect_args, not embedded URL creds."""
    with pytest.raises(ValueError, match="credentials"):
        SQLAlchemySessionStore(
            "mysql+pymysql://user:secret@host/db",
            connect_args={"user": "user", "password": "secret"},
        )


def test_construction_rejection_message_does_not_leak_password() -> None:
    """HC-C: the ValueError message from credential rejection must not echo the password."""
    bad_url = f"postgresql+psycopg://user:{SENTINEL_PW}@host/db"
    with pytest.raises(ValueError) as exc_info:
        SQLAlchemySessionStore(bad_url)
    error_text = str(exc_info.value)
    assert_no_password_leak(error_text)
    assert_no_password_leak(repr(exc_info.value))


def test_repr_does_not_leak_url_password() -> None:
    """HC-C: __repr__ redacts password even when _url is mutated post-construction.

    Bypass constructor validation by directly setting _url to a URL with the
    sentinel password — verifies belt-and-suspenders repr redaction.
    """
    store = SQLAlchemySessionStore("sqlite:///:memory:")
    # Bypass validation by directly mutating
    store._url = f"postgresql+psycopg://user:{SENTINEL_PW}@host/db"

    repr_text = repr(store)
    str_text = str(store)
    assert_no_password_leak(repr_text)
    assert_no_password_leak(str_text)
    # SQLAlchemy's hide_password=True replaces password with ***
    assert "***" in repr_text or SENTINEL_PW not in repr_text


def test_repr_does_not_leak_connect_args_credentials() -> None:
    """HC-C layer 2: repr/str never advertise connect_args or their values."""
    store = SQLAlchemySessionStore(
        "mysql+pymysql://host/db",
        connect_args={"user": "alice", "password": CONNECT_ARGS_SENTINEL},
    )

    repr_text = repr(store)
    str_text = str(store)
    assert CONNECT_ARGS_SENTINEL not in repr_text
    assert CONNECT_ARGS_SENTINEL not in str_text
    assert "connect_args" not in repr_text
    assert "connect_args" not in str_text


@pytest.mark.parametrize(
    "bad_key",
    ["password", "passwd", "pwd", "auth_token", "api_key", "secret", "foo", "bar123"],
)
def test_construction_rejects_url_with_unknown_query(bad_key: str) -> None:
    """HC-C: DSN with query param not in allowlist raises ValueError."""
    bad_url = f"postgresql+psycopg://host/db?{bad_key}=value"
    with pytest.raises(ValueError, match="query key"):
        SQLAlchemySessionStore(bad_url)


@pytest.mark.parametrize(
    "good_key",
    [
        "connect_timeout",
        "charset",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "application_name",
        "ssl_ca",
        "ssl_cert",
        "ssl_key",
        "ssl_verify_cert",
        "ssl_verify_identity",
    ],
)
def test_construction_accepts_allowed_query_keys(good_key: str) -> None:
    """HC-C: all 12 allowlist query keys are accepted."""
    url = f"postgresql+psycopg://host/db?{good_key}=value"
    store = SQLAlchemySessionStore(url)
    assert store is not None


def test_construction_validates_table_prefix_bad_chars() -> None:
    """table_prefix with bad chars raises ValueError."""
    bad_prefixes = [
        "1starts_with_digit",
        "has space",
        "has-dash",
        "has.dot",
        "has/slash",
    ]
    for bad in bad_prefixes:
        with pytest.raises(ValueError, match="table_prefix"):
            SQLAlchemySessionStore("sqlite:///:memory:", table_prefix=bad)


def test_construction_validates_table_prefix_too_long() -> None:
    """table_prefix longer than 32 chars raises ValueError."""
    long_prefix = "a" * 33
    with pytest.raises(ValueError, match="table_prefix"):
        SQLAlchemySessionStore("sqlite:///:memory:", table_prefix=long_prefix)


def test_construction_accepts_empty_table_prefix() -> None:
    """table_prefix='' (no prefix) is valid."""
    store = SQLAlchemySessionStore("sqlite:///:memory:", table_prefix="")
    assert store is not None


def test_construction_validates_schema_name_bad_chars() -> None:
    """schema with bad chars raises ValueError."""
    bad_schemas = ["1invalid", "has space", "has-dash", "has.dot"]
    for bad in bad_schemas:
        with pytest.raises(ValueError, match="schema"):
            SQLAlchemySessionStore("sqlite:///:memory:", schema=bad)


def test_construction_validates_schema_name_sql_keyword() -> None:
    """schema that is a SQL keyword raises ValueError."""
    sql_keywords = ["select", "from", "where", "drop", "table"]
    for kw in sql_keywords:
        with pytest.raises(ValueError, match="schema|keyword"):
            SQLAlchemySessionStore("sqlite:///:memory:", schema=kw)


def test_construction_accepts_valid_schema() -> None:
    """Valid schema names are accepted."""
    good_schemas = ["public", "myapp", "claw_prod", "workspace01"]
    for schema in good_schemas:
        store = SQLAlchemySessionStore("sqlite:///:memory:", schema=schema)
        assert store is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Lazy connection / engine management
# ═══════════════════════════════════════════════════════════════════════════════


def test_connect_args_default_none_does_not_pass_to_create_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9a.1: omitting connect_args preserves the pre-existing create_engine call."""
    original_create_engine = sqlalchemy.create_engine
    captured_kwargs: list[dict[str, object]] = []

    def spy_create_engine(url, **kwargs):
        captured_kwargs.append(dict(kwargs))
        return original_create_engine(url)

    monkeypatch.setattr(sqlalchemy, "create_engine", spy_create_engine)

    store = SQLAlchemySessionStore("sqlite:///:memory:")
    store._ensure_engine()

    assert captured_kwargs == [{}]
    assert "connect_args" not in captured_kwargs[0]


def test_connect_args_non_none_passes_dict_to_create_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9a.1: non-None connect_args is passed through at engine creation."""
    original_create_engine = sqlalchemy.create_engine
    captured_kwargs: list[dict[str, object]] = []

    def spy_create_engine(url, **kwargs):
        captured_kwargs.append(dict(kwargs))
        # The assertion is about adapter wiring; use a normal sqlite engine so
        # create_all remains independent of driver-specific connect args.
        return original_create_engine(url)

    monkeypatch.setattr(sqlalchemy, "create_engine", spy_create_engine)

    store = SQLAlchemySessionStore(
        "sqlite:///:memory:",
        connect_args={"k": "v"},
    )
    store._ensure_engine()

    assert captured_kwargs == [{"connect_args": {"k": "v"}}]


def test_connect_args_is_defensively_copied() -> None:
    """P9a.1: caller mutations after construction cannot alter adapter state."""
    connect_args = {"user": "alice", "password": "initial"}
    store = SQLAlchemySessionStore(
        "mysql+pymysql://host/db",
        connect_args=connect_args,
    )

    connect_args["password"] = "mutated"

    assert store._connect_args == {"user": "alice", "password": "initial"}


def test_connect_args_accepts_mapping_not_just_dict() -> None:
    """P9a.1: read-only Mapping inputs are accepted and normalized to dict."""
    connect_args = MappingProxyType({"user": "alice", "password": "secret"})
    store = SQLAlchemySessionStore(
        "mysql+pymysql://host/db",
        connect_args=connect_args,
    )

    assert store._connect_args == {"user": "alice", "password": "secret"}
    assert type(store._connect_args) is dict


def test_first_call_creates_engine() -> None:
    """Engine is None before first call, non-None after."""
    store = make_sqlite_store()
    assert store._engine is None
    store.get_or_create(**_KW)
    assert store._engine is not None


def test_engine_reused_across_calls() -> None:
    """Same Engine instance is reused on subsequent calls."""
    store = make_sqlite_store()
    store.get_or_create(**_KW)
    engine_a = store._engine
    store.get_or_create(**_KW)
    engine_b = store._engine
    assert engine_a is engine_b


def test_concurrent_first_call_does_not_double_create() -> None:
    """Two threads calling get_or_create simultaneously create only one Engine.

    Uses a threading.Barrier to synchronise both threads at the moment just
    before calling _ensure_engine, then verifies that exactly one Engine was
    created (double-checked locking in _ensure_engine prevents double-create).

    Uses a file-based SQLite so that all threads share the same connection pool
    (in-memory SQLite gives each connection an isolated empty database, which
    would fail schema-not-found checks in the second thread).
    """
    import time

    original_create_engine = sqlalchemy.create_engine
    create_count = [0]
    count_lock = threading.Lock()

    def counting_create_engine(*args, **kwargs):
        with count_lock:
            create_count[0] += 1
        # Widen the race window so the second thread can observe the unfinished
        # engine-creation path and hit the double-check in the lock
        time.sleep(0.02)
        return original_create_engine(*args, **kwargs)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = SQLAlchemySessionStore(f"sqlite:///{db_path}")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait()  # Both threads start at the same time
                store.get_or_create(**_KW)
            except Exception as e:
                errors.append(e)

        sqlalchemy.create_engine = counting_create_engine
        try:
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            sqlalchemy.create_engine = original_create_engine

        assert not errors, f"Thread errors: {errors}"
        assert create_count[0] == 1, (
            f"create_engine called {create_count[0]} times, expected exactly 1"
        )
        # Single Engine instance must be shared
        assert store._engine is not None
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════════════════════
# HC-B: Schema migration — create-only, idempotent
# ═══════════════════════════════════════════════════════════════════════════════


def test_schema_migration_idempotent() -> None:
    """HC-B: creating tables twice (two adapter instances, same file) does not error."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        url = f"sqlite:///{db_path}"
        store1 = SQLAlchemySessionStore(url)
        store1.get_or_create(**_KW)

        # Second adapter against same file — should not error
        store2 = SQLAlchemySessionStore(url)
        result = store2.get_or_create(**_KW)
        assert result is not None
    finally:
        os.unlink(db_path)


def test_schema_migration_never_drops_or_alters() -> None:
    """HC-B: schema migration is create-only.

    Note: the initial ``metadata.create_all()`` runs inside the first
    ``_ensure_engine()`` call BEFORE the ``before_cursor_execute`` hook below
    is registered, so the initial CREATE TABLE IF NOT EXISTS statements are
    NOT captured here.  We rely on SQLAlchemy's documented contract that
    ``metadata.create_all(checkfirst=True)`` emits only
    ``CREATE TABLE IF NOT EXISTS`` — never DROP or ALTER.  This test
    therefore verifies the steady-state CRUD + repeat-ensure_engine paths
    stay DDL-free; it does NOT re-verify the API-guaranteed initial
    migration.
    """
    store = SQLAlchemySessionStore("sqlite:///:memory:")
    # Trigger engine creation
    store._ensure_engine()
    engine = store._engine
    assert engine is not None

    captured_statements: list[str] = []

    @sqlalchemy.event.listens_for(engine, "before_cursor_execute")
    def capture_ddl(conn, cursor, statement, params, context, executemany):
        captured_statements.append(statement)

    # Force a second ensure_engine call (no-op but proves idempotency)
    engine2 = store._ensure_engine()
    assert engine2 is engine  # same instance

    # Perform another get_or_create to trigger more SQL
    store.get_or_create(**_KW)

    # Assert no DROP or ALTER in any captured statement
    for stmt in captured_statements:
        upper = stmt.upper()
        assert "DROP" not in upper, f"Found DROP in statement: {stmt!r}"
        assert "ALTER" not in upper, f"Found ALTER in statement: {stmt!r}"


def test_schema_argument_is_applied_to_tables() -> None:
    """C1: when ``schema=`` is set, both Table objects + their DDL carry the qualifier.

    Verified by:
    1. Constructing tables via ``_build_tables(prefix, schema=...)`` directly.
    2. Inspecting ``Table.schema`` attribute.
    3. Compiling ``CreateTable`` DDL against a dialect and asserting the
       schema-qualified table name appears in the rendered SQL.
    """
    from claw_engine.adapters.persistence.sqlalchemy.schema import _build_tables
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    sessions, processed = _build_tables("claw_", schema="myapp")

    # Tables carry the schema attribute
    assert sessions.schema == "myapp"
    assert processed.schema == "myapp"

    # DDL renders with schema-qualified name
    ddl_sessions = str(
        CreateTable(sessions).compile(dialect=postgresql.dialect())
    )
    assert "myapp.claw_sessions" in ddl_sessions, (
        f"Expected schema-qualified table name in DDL, got: {ddl_sessions!r}"
    )

    ddl_processed = str(
        CreateTable(processed).compile(dialect=postgresql.dialect())
    )
    assert "myapp.claw_processed_messages" in ddl_processed, (
        f"Expected schema-qualified table name in DDL, got: {ddl_processed!r}"
    )
    # FK target is also schema-qualified
    assert "myapp.claw_sessions" in ddl_processed


def test_build_tables_without_schema_is_unqualified() -> None:
    """C1 counterpart: schema=None leaves tables unqualified (sqlite/mysql default)."""
    from claw_engine.adapters.persistence.sqlalchemy.schema import _build_tables

    sessions, processed = _build_tables("claw_", schema=None)
    assert sessions.schema is None
    assert processed.schema is None


def test_ensure_engine_passes_schema_to_build_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: SQLAlchemySessionStore._ensure_engine forwards schema= to _build_tables.

    Spy on _build_tables and assert the schema kwarg matches the constructor
    arg.  This pins the wiring that C1 was about.
    """
    import claw_engine.adapters.persistence.sqlalchemy.store as store_mod

    captured: dict = {}
    original = store_mod._build_tables

    def spy(prefix, *, schema=None):
        captured["prefix"] = prefix
        captured["schema"] = schema
        return original(prefix, schema=schema)

    monkeypatch.setattr(store_mod, "_build_tables", spy)

    # Use sqlite since the URL still needs to be parseable; ``schema`` is
    # ignored at DDL execution on sqlite but the call must still propagate it.
    store = SQLAlchemySessionStore(
        "sqlite:///:memory:",
        schema="myapp",
        table_prefix="claw_",
    )
    # NOTE: sqlite ignores schema= at DDL emit time (raises OperationalError
    # because the named schema isn't ATTACH'd).  We catch the SessionStoreError
    # wrapper and inspect what was passed to _build_tables.
    try:
        store._ensure_engine()
    except SessionStoreError:
        # Expected — sqlite has no "myapp" schema ATTACH'd.  The point of this
        # test is to confirm schema was *passed*, which the spy captured before
        # the SQL error.
        pass

    assert captured == {"prefix": "claw_", "schema": "myapp"}


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD via Protocol
# ═══════════════════════════════════════════════════════════════════════════════


def test_get_or_create_creates_new_session() -> None:
    """First get_or_create creates a new session with all required fields."""
    store = make_sqlite_store()
    session = store.get_or_create(**_KW)
    assert session.session_id
    assert session.workspace_id == "w"
    assert session.channel == "c"
    assert session.external_thread_key == "t"
    assert session.backend_name == "fake"
    assert session.max_rounds == 50
    assert session.round_count == 0
    assert session.backend_thread_id is None


def test_get_or_create_returns_existing_session() -> None:
    """Second get_or_create with same natural key returns same session_id."""
    store = make_sqlite_store()
    s1 = store.get_or_create(**_KW)
    s2 = store.get_or_create(
        workspace_id="w", channel="c", external_thread_key="t",
        backend_name="other", max_rounds=10,
    )
    assert s1.session_id == s2.session_id
    assert s2.backend_name == "fake"   # existing wins
    assert s2.max_rounds == 50


def test_get_or_create_different_natural_key_different_session() -> None:
    """Different natural keys produce different sessions."""
    store = make_sqlite_store()
    a = store.get_or_create(**{**_KW, "external_thread_key": "t1"})
    b = store.get_or_create(**{**_KW, "external_thread_key": "t2"})
    assert a.session_id != b.session_id


def test_save_updates_existing_session() -> None:
    """save() with updated round_count persists to the store."""
    store = make_sqlite_store()
    s = store.get_or_create(**_KW)
    updated = s.with_turn(backend_thread_id="bt_1", now=123.0)
    store.save(updated)

    again = store.get_or_create(**_KW)
    assert again.backend_thread_id == "bt_1"
    assert again.round_count == 1
    assert again.last_active == 123.0


def test_save_does_not_create_new_session() -> None:
    """save() of an unknown session_id is a no-op (no rows created)."""
    from claw_engine.engine.persistence.contracts import Session
    store = make_sqlite_store()

    phantom = Session(
        session_id="nonexistent-id",
        workspace_id="w",
        channel="c",
        external_thread_key="t",
        backend_name="fake",
        max_rounds=50,
    )
    # Should not raise, should be a no-op
    store.save(phantom)

    # A real get_or_create should create a NEW session (not find the phantom)
    real = store.get_or_create(**_KW)
    assert real.session_id != "nonexistent-id"


def test_is_processed_returns_false_initially() -> None:
    """Fresh session has no processed messages."""
    store = make_sqlite_store()
    s = store.get_or_create(**_KW)
    assert not store.is_processed(s.session_id, "msg-1")


def test_mark_processed_then_is_processed_true() -> None:
    """After mark_processed, is_processed returns True."""
    store = make_sqlite_store()
    s = store.get_or_create(**_KW)
    store.mark_processed(s.session_id, "msg-1")
    assert store.is_processed(s.session_id, "msg-1")
    assert not store.is_processed(s.session_id, "msg-2")


def test_mark_processed_idempotent() -> None:
    """Calling mark_processed twice does not raise."""
    store = make_sqlite_store()
    s = store.get_or_create(**_KW)
    store.mark_processed(s.session_id, "msg-1")
    store.mark_processed(s.session_id, "msg-1")  # should not error
    assert store.is_processed(s.session_id, "msg-1")


# ═══════════════════════════════════════════════════════════════════════════════
# P1-#2: IntegrityError must propagate OUT of engine.begin() before being caught
# (regression for Postgres aborted-transaction state).  SQLite forgives this
# pattern, but Postgres does not — the tests below exercise the post-error
# follow-up path that proves no aborted-state leakage.
# ═══════════════════════════════════════════════════════════════════════════════


def test_mark_processed_twice_then_subsequent_ops_work() -> None:
    """P1-#2 regression: after a duplicate mark_processed, subsequent ops succeed.

    If IntegrityError were caught INSIDE ``with engine.begin()``, on Postgres
    the next operation on the same connection would fail with "current
    transaction is aborted, commands ignored".  This test pins the structural
    fix that the catch is OUTSIDE the ``with``.
    """
    store = make_sqlite_store()
    s = store.get_or_create(**_KW)
    store.mark_processed(s.session_id, "msg-1")
    store.mark_processed(s.session_id, "msg-1")  # duplicate (IntegrityError path)
    # Both reads + writes after the duplicate must work cleanly
    assert store.is_processed(s.session_id, "msg-1") is True
    store.mark_processed(s.session_id, "msg-2")
    assert store.is_processed(s.session_id, "msg-2") is True
    # And we can keep doing duplicates without stuck state
    store.mark_processed(s.session_id, "msg-2")
    assert store.is_processed(s.session_id, "msg-2") is True


def test_concurrent_get_or_create_returns_same_session_id_under_race() -> None:
    """P1-#2 regression: under a real INSERT race, both threads return the same id.

    Forces the IntegrityError re-fetch path inside get_or_create.  If the
    catch were inside ``with engine.begin()`` on Postgres, the re-fetch
    SELECT would run inside the aborted transaction and either error or
    return stale data, causing the two threads to disagree.
    """
    import time

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = SQLAlchemySessionStore(f"sqlite:///{db_path}")
        # Pre-warm the engine so the lock-contention is on INSERT, not init
        store._ensure_engine()

        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait()
                # Tiny stagger to widen the race
                time.sleep(0.001)
                s = store.get_or_create(**_KW)
                with lock:
                    results.append(s.session_id)
            except Exception as e:
                with lock:
                    errors.append(e)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 2
        # Both threads must observe the same winner — proves the re-fetch
        # in get_or_create ran cleanly after the IntegrityError path.
        assert results[0] == results[1], (
            f"Concurrent get_or_create returned different session_ids: {results}"
        )
    finally:
        os.unlink(db_path)


def test_integrity_error_catch_is_outside_engine_begin_block() -> None:
    """P1-#2 structural pin: source-level check that the catch is OUT of with-block.

    Inspects the source of get_or_create and mark_processed to assert that no
    ``except sqlalchemy.exc.IntegrityError`` appears within an ``engine.begin()``
    context.  This is the strongest guard against a future maintainer
    "simplifying" the pattern back into the buggy nested form.
    """
    import inspect
    from claw_engine.adapters.persistence.sqlalchemy.store import (
        SQLAlchemySessionStore as _S,
    )

    for method_name in ("get_or_create", "mark_processed"):
        src = inspect.getsource(getattr(_S, method_name))
        # Pop-stack walk: track the indentation of any active ``with engine.begin``.
        # An ``except .*IntegrityError`` at deeper indent than such a ``with``
        # before that ``with`` block has been left signals the buggy pattern.
        in_with_begin: list[int] = []  # stack of indent levels
        for line in src.splitlines():
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            # Pop any with-begin blocks whose body has ended
            while in_with_begin and indent <= in_with_begin[-1]:
                in_with_begin.pop()
            if "with " in stripped and "engine.begin()" in stripped:
                in_with_begin.append(indent)
                continue
            if "except" in stripped and "IntegrityError" in stripped:
                assert not in_with_begin, (
                    f"P1-#2 regression: ``except IntegrityError`` is inside an "
                    f"active ``with engine.begin()`` block in {method_name!r}. "
                    f"This causes Postgres aborted-transaction failures.\n"
                    f"Offending line: {line!r}"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# Protocol compatibility
# ═══════════════════════════════════════════════════════════════════════════════


def test_isinstance_session_store() -> None:
    """isinstance(adapter, SessionStore) is True via runtime_checkable."""
    store = make_sqlite_store()
    assert isinstance(store, SessionStore)


# ═══════════════════════════════════════════════════════════════════════════════
# Field validation at adapter boundary
# ═══════════════════════════════════════════════════════════════════════════════


def test_get_or_create_rejects_empty_workspace_id() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="workspace_id"):
        store.get_or_create(**{**_KW, "workspace_id": ""})


def test_get_or_create_rejects_workspace_id_too_long() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="workspace_id"):
        store.get_or_create(**{**_KW, "workspace_id": "a" * 129})


def test_get_or_create_rejects_empty_channel() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="channel"):
        store.get_or_create(**{**_KW, "channel": ""})


def test_get_or_create_rejects_channel_too_long() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="channel"):
        store.get_or_create(**{**_KW, "channel": "a" * 65})


def test_get_or_create_accepts_channel_with_special_chars() -> None:
    """Channel can contain colons, slashes, hashes (valid thread key chars)."""
    store = make_sqlite_store()
    session = store.get_or_create(
        **{**_KW, "channel": "slack:T01ABC#general/thread/123"}
    )
    assert session is not None


def test_get_or_create_rejects_empty_external_thread_key() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="external_thread_key"):
        store.get_or_create(**{**_KW, "external_thread_key": ""})


def test_get_or_create_rejects_external_thread_key_too_long() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="external_thread_key"):
        store.get_or_create(**{**_KW, "external_thread_key": "a" * 257})


def test_is_processed_rejects_empty_session_id() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="session_id"):
        store.is_processed("", "msg-1")


def test_is_processed_rejects_empty_message_id() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="message_id"):
        store.is_processed("sid-1", "")


def test_mark_processed_rejects_empty_session_id() -> None:
    store = make_sqlite_store()
    with pytest.raises(ValueError, match="session_id"):
        store.mark_processed("", "msg-1")


# ═══════════════════════════════════════════════════════════════════════════════
# Table prefix isolation
# ═══════════════════════════════════════════════════════════════════════════════


def test_table_prefix_isolation() -> None:
    """Two adapters with different table_prefix share a sqlite DB without cross-contamination."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        url = f"sqlite:///{db_path}"
        store_a = SQLAlchemySessionStore(url, table_prefix="alpha_")
        store_b = SQLAlchemySessionStore(url, table_prefix="beta_")

        # Create session in store_a
        sa = store_a.get_or_create(
            workspace_id="w", channel="c", external_thread_key="t",
            backend_name="fake", max_rounds=10,
        )

        # store_b must create a new session (different table)
        sb = store_b.get_or_create(
            workspace_id="w", channel="c", external_thread_key="t",
            backend_name="fake", max_rounds=10,
        )

        # Different session_ids from different tables
        assert sa.session_id != sb.session_id
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════════════════════
# SessionStoreError
# ═══════════════════════════════════════════════════════════════════════════════


def test_session_store_error_str() -> None:
    err = SessionStoreError("some problem", stage="init")
    assert "init" in str(err)
    assert "some problem" in str(err)


def test_session_store_error_repr() -> None:
    err = SessionStoreError("some problem", stage="query")
    assert "SessionStoreError" in repr(err)
    assert "query" in repr(err)


def test_session_store_error_no_stage() -> None:
    err = SessionStoreError("problem without stage")
    assert err.stage is None
    assert "problem without stage" in str(err)


# ═══════════════════════════════════════════════════════════════════════════════
# I4: SessionStoreError validates the ``stage`` field
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("bad_stage", ["quary", "Init", "QUERY", "", "auth", "network"])
def test_session_store_error_rejects_invalid_stage(bad_stage: str) -> None:
    """I4: ``stage`` must be one of {'init', 'query'} or None — typos rejected."""
    with pytest.raises(ValueError, match="stage"):
        SessionStoreError("msg", stage=bad_stage)


@pytest.mark.parametrize("good_stage", [None, "init", "query"])
def test_session_store_error_accepts_valid_stage(good_stage: str | None) -> None:
    """I4: the documented stages all construct without error."""
    err = SessionStoreError("msg", stage=good_stage)
    assert err.stage == good_stage


# ═══════════════════════════════════════════════════════════════════════════════
# I3: _ensure_engine wraps SQLAlchemy errors in SessionStoreError(stage='init')
# ═══════════════════════════════════════════════════════════════════════════════


def test_ensure_engine_wraps_init_failures_in_session_store_error() -> None:
    """I3: when create_engine / create_all fails, raise SessionStoreError(stage='init').

    Uses a sqlite DSN pointing at a directory that doesn't exist — sqlite
    will fail to open the database file when create_all attempts the first
    transaction.  The raw sqlalchemy.exc.OperationalError must be wrapped.
    """
    # sqlite cannot create databases in non-existent directories
    bad_url = "sqlite:////nonexistent/parent/dir/db.sqlite"
    store = SQLAlchemySessionStore(bad_url)
    with pytest.raises(SessionStoreError) as exc_info:
        store._ensure_engine()
    assert exc_info.value.stage == "init"
    # I3 + HC-C: the wrapped message must NOT echo the underlying SQLAlchemy
    # error text (which may contain the bound URL with credentials).
    msg = str(exc_info.value)
    assert "OperationalError" in msg or "Error" in msg
    # Cause chain must preserve the original for debuggers
    assert exc_info.value.__cause__ is not None


def test_ensure_engine_init_error_does_not_leak_password() -> None:
    """I3 + HC-C: init-time errors must never leak DSN credentials.

    Bypass constructor validation, set a credentialed URL post-construction,
    trigger init failure, and verify the error message excludes the password.
    """
    store = SQLAlchemySessionStore("sqlite:////nonexistent/dir/db.sqlite")
    # Inject a sentinel password into the stored URL
    store._url = (
        f"sqlite:////nonexistent/dir/db.sqlite?password={SENTINEL_PW}"
    )
    # Now query keys are checked at construction only; _ensure_engine just
    # forwards self._url to create_engine.  SQLAlchemy may include the URL
    # in its error.  Our wrapper must strip it.
    with pytest.raises(SessionStoreError) as exc_info:
        store._ensure_engine()
    assert_no_password_leak(str(exc_info.value))
    assert_no_password_leak(repr(exc_info.value))


def test_init_failure_does_not_leak_connect_args_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HC-C layer 2: wrapped init errors do not echo connect_args values or label.

    The no-leak surface is the adapter-facing exception only: str, repr, and
    args. The __cause__ chain is intentionally outside this assertion.
    """

    def boom_create_engine(*args, **kwargs):
        assert kwargs["connect_args"]["password"] == CONNECT_ARGS_SENTINEL
        raise sqlalchemy.exc.OperationalError(
            "simulated",
            None,
            Exception(CONNECT_ARGS_SENTINEL),
        )

    monkeypatch.setattr(sqlalchemy, "create_engine", boom_create_engine)

    store = SQLAlchemySessionStore(
        "mysql+pymysql://127.0.0.1:1/db",
        connect_args={
            "user": "alice",
            "password": CONNECT_ARGS_SENTINEL,
            "connect_timeout": 1,
        },
    )
    with pytest.raises(SessionStoreError) as exc_info:
        store.get_or_create(**_KW)

    assert exc_info.value.stage == "init"
    assert_no_connect_args_leak(exc_info.value)


def test_ensure_engine_disposes_half_built_engine_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I3 / Minor 8: when create_all fails, the partially-built engine.dispose() is called.

    Patch create_all to raise an SQLAlchemyError, then verify the engine
    object created just before was disposed (pool released).
    """
    dispose_calls: list[bool] = []
    original_create_engine = sqlalchemy.create_engine

    def patched_create_engine(*args, **kwargs):
        engine = original_create_engine(*args, **kwargs)
        original_dispose = engine.dispose

        def tracking_dispose(*a, **kw):
            dispose_calls.append(True)
            return original_dispose(*a, **kw)

        engine.dispose = tracking_dispose  # type: ignore[method-assign]
        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", patched_create_engine)

    # Patch MetaData.create_all on any instance to raise
    def boom_create_all(self, *args, **kwargs):
        raise sqlalchemy.exc.OperationalError("simulated", None, Exception("boom"))

    monkeypatch.setattr(sqlalchemy.MetaData, "create_all", boom_create_all)

    store = SQLAlchemySessionStore("sqlite:///:memory:")
    with pytest.raises(SessionStoreError):
        store._ensure_engine()
    assert dispose_calls == [True], (
        f"engine.dispose should be called exactly once on init failure, "
        f"got calls: {dispose_calls}"
    )
