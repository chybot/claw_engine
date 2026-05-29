# P9a: testcontainers Infra + P8e SQLAlchemy Integration

**Parent context:** Follow-up to P8 (`/Users/lucas.xu/.claude/plans/cryptic-twirling-sonnet.md`).
**Baseline:** main @ `c7a4124` (P8 complete, tag `v1.1.0`).
**Branch:** `feat/p9a-sqlalchemy-integration`.

---

## Why this sub-plan exists

P8e P1-#2 surfaced a real production bug — `try/except IntegrityError` inside `with engine.begin()`
worked on SQLite but breaks on PostgreSQL (aborted-transaction state). The fix is structurally
correct (source-inspection regression test guards against reintroduction) and the concurrent-race
semantics are verified on SQLite, but **real Postgres semantics are still untested**. P9a closes
that loop.

User-scoped P9a tightly: **testcontainers infra + P8e only**. OpenBao integration (P8d) has a
completely different failure profile (HTTP / auth / network) and goes to P9b. Mixing infra setup,
DB transaction semantics, and HTTP secret-backend bugs in one PR would make debugging harder.

---

## 1. Scope (locked)

### In scope (P9a)

- `testcontainers` library wiring as `[integration]` optional extra (added to pyproject)
- `tests/integration/conftest.py` with shared **Postgres** fixtures (MySQL deferred — see §1.1)
- Activate the 3 Postgres-side stubs in `tests/integration/test_sqlalchemy_session_store_integration.py`
- Extend the existing **runner** `tests/contract/test_sessionstore_contract.py` to parametrise
  `sqlalchemy-postgres` when integration profile is on. The **helper**
  `tests/contract/sessionstore_contract.py` (the assertion functions taking
  `Callable[[], SessionStore]`) is **untouched**.
- 3 mandatory test categories (concurrent race, duplicate idempotency, cross-restart durability)
- Explicit P1-#2 verification on **real Postgres** (the test that would have caught the original bug;
  MySQL's InnoDB does not have aborted-transaction semantics, so this verification is Postgres-specific)
- Document `pytest -m integration` invocation in README

### Out of scope (deferred)

- **MySQL integration → P9a.1 follow-up** (requires a small adapter extension; see §1.1)
- **OpenBao integration tests** → P9b separate plan
- **Engine contract changes** — locked, P8 principle continues
- **SessionStore Protocol changes** — same
- **Async variant** — sub-plan §13 of P8e defers; still deferred
- **CI automation** (nightly job, GitHub Actions integration matrix) — separate infra plan
- **MariaDB**, **Oracle**, **MSSQL** — V1 ships Postgres + MySQL only
- **Performance benchmarks** — correctness first, perf later

## 1.1 Why MySQL is deferred to P9a.1 (credential-free constraint)

P8e locked two constraints that together block real-MySQL integration:

1. **HC-C** rejects DSN with embedded username/password (`postgresql+psycopg://user:pass@host` → ValueError)
2. **DSN query allowlist** is locked to `{connect_timeout, charset, sslmode, sslrootcert, sslcert,
   sslkey, application_name, ssl_ca, ssl_cert, ssl_key, ssl_verify_cert, ssl_verify_identity}` — does
   not include `user`, `password`, `read_default_file`, `read_default_group`

**Postgres has an escape hatch**: `psycopg` / libpq respects `PGUSER`, `PGPASSWORD`, `PGHOST`,
`PGPORT`, `PGDATABASE` env vars when the DSN omits userinfo. The integration fixture sets these
env vars from the testcontainers-generated values and constructs a DSN like
`postgresql+psycopg://localhost:54321/test` — passes HC-C, libpq reads creds from env.

**MySQL has no equivalent**. `pymysql` does NOT consult env vars for credentials. Possible paths,
each blocked:

| Path | Blocker |
|---|---|
| `mysql+pymysql://root@host` (username only) | HC-C rejects any embedded username |
| `mysql+pymysql://host/db?user=root` (query) | `user` not in query allowlist |
| `?read_default_file=/tmp/my.cnf` (cnf path) | `read_default_file` not in query allowlist |
| `mysql:8.0` with `MYSQL_ALLOW_EMPTY_PASSWORD=yes` + no userinfo | pymysql still needs a username — defaults to OS user, won't match `root` |

The genuinely correct fix is to **add `connect_args: dict | None = None` kwarg to
`SQLAlchemySessionStore.__init__`** so callers can pass `connect_args={"user": "root", "password": "..."}`
to SQLAlchemy `create_engine`. This is a small additive adapter change (no Protocol change, no engine
change, no HC-C weakening since `connect_args` is a different surface from URL credentials), but it
**is** an adapter change.

**Decision**: P9a stays "no adapter change". P9a.1 follow-up adds `connect_args` kwarg + MySQL
integration. Reasons:

- P1-#2 killer test (the most important goal of P9a) targets Postgres aborted-transaction
  semantics. MySQL InnoDB uses different concurrency (gap locks, no aborted-transaction state),
  so the killer test is intrinsically Postgres-specific. P9a achieves its main goal without MySQL.
- Splitting keeps the credential-free Postgres path clean from the adapter-extension MySQL path
- Less to review per PR; easier to roll back if `connect_args` interacts poorly with existing
  HC-A/HC-C tests

---

## 2. Testcontainers Infrastructure

### 2.1 Library choice (locked)

Use **`testcontainers`** Python package (`pip install testcontainers[postgres]` — drop `mysql`
extras since MySQL is deferred to P9a.1). Reasons:
- Maintained, ~3M downloads/mo, supports Postgres out of box
- Sync API matches our adapter's sync Protocol — no asyncio mismatch
- Built on Docker SDK; works on macOS / Linux / Docker Desktop / CI runners with Docker
- Same library powers most Python integration test suites in the wider ecosystem

### 2.2 Container images (locked)

- **Postgres**: `postgres:16.4-alpine` — pinned to a **specific minor** for full reproducibility.
  `postgres:16-alpine` would float minor versions and could change behavior on test reruns; alpine
  variant is small (~80MB) and pulls fast. Bump explicitly in a follow-up when ready, not via a
  floating tag. (Implementer: verify the chosen minor is published; bump to a current one if not.)
- **MySQL**: deferred to P9a.1 (see §1.1)

If postgres 17 ships and we want to upgrade, that's a separate PR with explicit version bump
and any compatibility fixes.

### 2.3 Container lifecycle (locked: session-scoped + per-test isolation)

Two-level fixture design:

| Level | Scope | Lifetime | Cost |
|---|---|---|---|
| **Container** | `session` | Started once per `pytest` invocation, reused across all tests | ~3-5s startup (cached image) |
| **Schema/prefix isolation** | `function` | Each test gets its own `table_prefix` (e.g. `test_<uuid8>_`) | ~50ms |

Why per-prefix isolation instead of per-test DROP/CREATE: faster (no DDL between tests) +
exercises the adapter's `table_prefix` feature in the integration path. Tests are independent
because no two tests share a prefix.

### 2.4 Skip behavior when Docker unavailable + default-CI command

- `tests/integration/conftest.py` uses `pytest.importorskip("testcontainers")` at module top
- If `testcontainers` is installed but Docker daemon is missing/unreachable, fixture catches the
  exception and `pytest.skip("Docker daemon not available")`
- **Default CI must explicitly filter**: pyproject does NOT set `addopts = "-m 'not integration'"`,
  so bare `pytest -q` will collect integration-marked tests. Default CI invocation MUST be
  **`pytest -q -m "not integration"`** to skip them. The marker is registered in pyproject
  (`[tool.pytest.ini_options].markers`) so pytest won't warn about the filter expression.
- Integration CI (`-m integration`) skips gracefully when Docker missing — doesn't fail the run

Document expected behaviors in conftest docstring AND in `tests/integration/README.md`.

### 2.5 Container startup timeout

- Default: 60s (covers slow CI pull + cold start)
- Configurable via env var `CLAW_TC_TIMEOUT_SECONDS` for slower runners
- Past 60s → fixture raises `TimeoutError`, test marked errored (not silently skipped — explicit
  failure)

---

## 3. Fixture Design

`tests/integration/conftest.py` exposes 3 fixtures (MySQL fixtures deferred to P9a.1):

```python
@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Start a Postgres container once per pytest session, reuse across tests."""
    ...

@pytest.fixture
def postgres_url(postgres_container, monkeypatch) -> str:
    """Returns a credential-free DSN like 'postgresql+psycopg://localhost:54321/test'
    AND sets PGUSER + PGPASSWORD env vars so psycopg/libpq picks them up at connect time.
    
    This is required because P8e HC-C rejects DSNs with embedded user:pass@. We rely on
    libpq's documented env-var fallback. See sub-plan §1.1 for the full rationale.
    """
    # Pull testcontainers-generated creds (default: test/test)
    user = postgres_container.username
    password = postgres_container.password
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    db = postgres_container.dbname
    
    # Set env vars so libpq reads them (monkeypatch auto-restores after test)
    monkeypatch.setenv("PGUSER", user)
    monkeypatch.setenv("PGPASSWORD", password)
    
    # Construct credential-free DSN (no user:pass@); host + port + db only
    return f"postgresql+psycopg://{host}:{port}/{db}"

@pytest.fixture
def unique_table_prefix() -> str:
    """Generate 'test_<8 hex chars>_' for per-test table isolation."""
    return f"test_{uuid.uuid4().hex[:8]}_"
```

**Critical**: `monkeypatch.setenv` is function-scoped — the env vars get auto-restored after
each test, so concurrent test runs (if pytest-xdist is added later) don't cross-pollute. Same
fixture pattern works for cron-style integration runs.

Note: `testcontainers.postgres.PostgresContainer.get_connection_url()` returns
`postgresql+psycopg2://user:pass@host:port/db` by default — we do NOT use that helper. We
construct our own credential-free URL from container properties.

### Sanity test for the fixture itself

```python
@pytest.mark.integration
def test_postgres_fixture_url_passes_p8e_validation(postgres_url):
    """The fixture URL must be acceptable to SQLAlchemySessionStore constructor —
    no embedded credentials, no forbidden query keys, scheme is 'postgresql+psycopg'."""
    # Construction must not raise (HC-A no IO, HC-C no creds)
    store = SQLAlchemySessionStore(postgres_url)
    # First Protocol call exercises actual connection via env-var auth
    session = store.get_or_create(
        workspace_id="ws", channel="slack", external_thread_key="t",
        backend_name="codex", max_rounds=50,
    )
    assert session.session_id  # actual auth + connection succeeded
```

---

## 4. Test Categories (mandatory)

`tests/integration/test_sqlalchemy_session_store_integration.py` — replace the 3 Postgres stubs
with real implementations + add the schema test (§4.5) + the fixture sanity test (§3 end). All
marked `@pytest.mark.integration`. MySQL stubs are either removed or marked `pytest.skip("→ P9a.1")`.

### 4.1 Concurrent `get_or_create` race (P1-#2 verification — the killer test)

**Postgres only** (MySQL InnoDB lacks aborted-transaction state; bug not reproducible there):

```python
@pytest.mark.integration
def test_concurrent_get_or_create_deterministically_triggers_integrity_error_on_postgres(
    postgres_url, unique_table_prefix
):
    """P1-#2 killer test: TWO threads must DETERMINISTICALLY race INSERT (not just
    'start at same time'). One wins → other gets IntegrityError → adapter must
    re-fetch in a fresh transaction. AND the post-race connection pool must NOT
    be in aborted-transaction state.
    
    Plain threading.Barrier is NOT sufficient: a fast first thread could complete
    INSERT before the second thread's SELECT, in which case the second thread sees
    the existing row and never tries INSERT — no IntegrityError, bug not exercised.
    
    Deterministic approach: register a SQLAlchemy 'before_cursor_execute' event
    that blocks both threads at a Barrier right BEFORE the INSERT statement (after
    each has done its SELECT). Both threads then proceed to INSERT simultaneously,
    forcing the second to hit UNIQUE-constraint violation.
    """
    import threading
    from sqlalchemy import event
    
    store = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    
    # PREWARM: trigger lazy _ensure_engine BEFORE attaching events, so the engine
    # exists and the event hook is registered on the right Engine instance.
    # Use a throwaway natural key so the test natural key has fresh state.
    store.get_or_create(
        workspace_id="prewarm", channel="x", external_thread_key="x",
        backend_name="codex", max_rounds=50,
    )
    
    insert_barrier = threading.Barrier(2, timeout=10)
    
    @event.listens_for(store._engine, "before_cursor_execute")
    def block_inserts_until_both_arrive(conn, cursor, statement, params, context, executemany):
        # Block ONLY on the sessions-table INSERT, not on every statement
        if statement.lstrip().upper().startswith("INSERT INTO") and f"{unique_table_prefix}sessions" in statement:
            insert_barrier.wait()
    
    natural_key = dict(
        workspace_id="ws-race", channel="slack",
        external_thread_key="thread-race",
        backend_name="codex", max_rounds=50,
    )
    
    results: list = [None, None]
    errors: list = [None, None]
    
    def racer(idx: int) -> None:
        try:
            results[idx] = store.get_or_create(**natural_key)
        except Exception as exc:
            errors[idx] = exc
    
    t1 = threading.Thread(target=racer, args=(0,))
    t2 = threading.Thread(target=racer, args=(1,))
    try:
        t1.start(); t2.start()
        t1.join(); t2.join()
    finally:
        # CRITICAL: remove the event listener BEFORE the post-race assertions
        # below. Otherwise mark_processed / fresh get_or_create also issue
        # INSERTs into the same table — only the main thread reaches the
        # barrier, barrier.wait() times out, test deadlocks.
        event.remove(store._engine, "before_cursor_execute", block_inserts_until_both_arrive)
    
    # Both threads completed without escaping exception
    assert errors == [None, None], f"unexpected exceptions: {errors}"
    
    # Both returned the same session_id (the winner's)
    assert results[0] is not None and results[1] is not None
    assert results[0].session_id == results[1].session_id
    
    # CRITICAL P1-#2 ASSERTION: post-race connection pool must work.
    # If the race left a connection in aborted-transaction state, the next
    # write would fail with Postgres error "current transaction is aborted,
    # commands ignored until end of transaction block".
    # (Event listener already removed above, so these INSERTs don't hit barrier.)
    session = results[0]
    store.mark_processed(session.session_id, "msg-after-race")
    assert store.is_processed(session.session_id, "msg-after-race") is True
    
    # Also: a completely fresh get_or_create on different natural key
    fresh = store.get_or_create(
        workspace_id="ws-2", channel="slack", external_thread_key="thread-Y",
        backend_name="codex", max_rounds=50,
    )
    assert fresh.session_id != session.session_id
```

The `before_cursor_execute` event hook + per-INSERT barrier is the standard SQLAlchemy pattern
for deterministic race testing. The `unique_table_prefix` substring match avoids false-blocking
on unrelated INSERTs (e.g. the prewarm call which used different natural key but same table —
prewarm completes BEFORE the hook is attached, so it's safe; fresh-key call comes AFTER the
hook is REMOVED, so also safe).

**Why `event.remove` (not a one-shot counter)**: explicit teardown via `finally` is symmetric with
`event.listens_for` registration and obvious to a future reader. A one-shot counter (e.g.
`if insert_count <= 2: barrier.wait()`) would also work but adds shared mutable state without
a corresponding visible cleanup.

### 4.2 Duplicate `mark_processed` idempotency (Postgres)

```python
@pytest.mark.integration
def test_duplicate_mark_processed_does_not_break_subsequent_ops(
    postgres_url, unique_table_prefix
):
    """P1-#2 sibling: duplicate mark_processed must not leave conn in aborted state.
    Subsequent is_processed + mark_processed of different message must succeed."""
    store = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    session = store.get_or_create(
        workspace_id="ws", channel="slack", external_thread_key="t",
        backend_name="codex", max_rounds=50,
    )
    
    store.mark_processed(session.session_id, "msg-1")
    store.mark_processed(session.session_id, "msg-1")  # duplicate — triggers IntegrityError path
    
    # If P1-#2 regressed, the next line would raise on Postgres
    assert store.is_processed(session.session_id, "msg-1") is True
    store.mark_processed(session.session_id, "msg-2")  # fresh msg, must succeed
    assert store.is_processed(session.session_id, "msg-2") is True
```

### 4.3 Cross-restart durability (Postgres)

```python
@pytest.mark.integration
def test_cross_restart_session_persists(postgres_url, unique_table_prefix):
    """Container survives between two SQLAlchemySessionStore instances; data persists."""
    # Phase 1: create + save + dispose
    store1 = SQLAlchemySessionStore(postgres_url, table_prefix=unique_table_prefix)
    session = store1.get_or_create(
        workspace_id="ws", channel="slack", external_thread_key="t",
        backend_name="codex", max_rounds=50,
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
        workspace_id="ws", channel="slack", external_thread_key="t",
        backend_name="codex", max_rounds=50,
    )
    assert rehydrated.session_id == saved_id
    assert rehydrated.backend_thread_id == "backend-thread-1"
    assert rehydrated.round_count == 1
    assert store2.is_processed(saved_id, "msg-1") is True
```

### 4.4 Cross-backend contract suite extension

Extend the **existing** contract runner `tests/contract/test_sessionstore_contract.py` (which
imports assertions from helper `tests/contract/sessionstore_contract.py`) to include
`sqlalchemy-postgres` ONLY when the integration profile is active.

**Important pytest -m semantics correction**: `pytest -m integration` runs ONLY tests with the
`integration` marker — it does NOT also run unmarked tests. So the marker design must be:

| Command | Cases run **in `test_sessionstore_contract.py` only** |
|---|---|
| `pytest -q -m "not integration"` (default CI) | 3 baseline backends × 4 assertions = 12 cases |
| `pytest -q -m integration` (integration-only) | 1 sqlalchemy-postgres backend × 4 assertions = 4 cases |
| `pytest -q` (no filter) | 12 baseline + 4 integration = **16 cases**, requires Docker |

This table is scoped to the contract suite file alone. Whole-repo case counts (which include the
5 new integration-suite tests in `test_sqlalchemy_session_store_integration.py`) are in §8. The
9 integration-marked cases under Docker = 4 here + 5 in the integration suite file.

(Note: not "5 backends = 20 cases" as the original draft incorrectly stated. There are 3 baseline +
1 integration = 4 backends in P9a. After P9a.1 adds MySQL, the contract-suite integration-profile
count would be 2 backends × 4 = 8 cases.)

**Fixture-aware parametrize** — the existing runner uses `STORES = [(name, factory), ...]` with
`@pytest.mark.parametrize("name,make_store", STORES, ids=...)`. For postgres, the factory needs
fixture-resolved URL+prefix, which can't be built at module-load time. **Restructure runner** to
use indirect parametrize via a fixture that returns the factory (still satisfying helper's
`Callable[[], SessionStore]` contract):

```python
# tests/contract/test_sessionstore_contract.py — REVISED structure

import pytest
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
from tests.contract import sessionstore_contract as sc

try:
    from claw_engine.adapters.persistence.sqlalchemy import SQLAlchemySessionStore
    _SA_AVAILABLE = True
except ImportError:
    _SA_AVAILABLE = False


_BACKENDS = [
    "memory",
    "sqlite",
    pytest.param("sqlalchemy-sqlite", marks=pytest.mark.skipif(
        not _SA_AVAILABLE, reason="sqlalchemy not installed")),
    pytest.param("sqlalchemy-postgres", marks=[
        pytest.mark.integration,
        pytest.mark.skipif(not _SA_AVAILABLE, reason="sqlalchemy not installed"),
    ]),
]


@pytest.fixture(params=_BACKENDS)
def make_store(request, tmp_path):
    """Return a Callable[[], SessionStore] factory matching the helper's contract.
    
    For postgres, fixtures postgres_url + unique_table_prefix from
    tests/integration/conftest.py are loaded only when the postgres case is active.
    """
    backend = request.param
    if backend == "memory":
        return lambda: MemorySessionStore()
    if backend == "sqlite":
        return lambda: SqliteSessionStore(":memory:")
    if backend == "sqlalchemy-sqlite":
        # Each factory call yields a store against the SAME in-memory DB so the
        # helper's two-call patterns (e.g. assert_save_persists_turn) see persistence
        # within a single test. Note: sqlite ":memory:" without shared cache means
        # each new SQLAlchemy engine gets its own DB. For sqlite-sqlalchemy, keep
        # current behaviour: each test's make_store closure creates one engine and
        # returns the SAME store on every call.
        store = SQLAlchemySessionStore("sqlite:///:memory:")
        return lambda: store
    if backend == "sqlalchemy-postgres":
        url = request.getfixturevalue("postgres_url")
        prefix = request.getfixturevalue("unique_table_prefix")
        store = SQLAlchemySessionStore(url, table_prefix=prefix)
        return lambda: store
    raise ValueError(f"unknown backend {backend!r}")


def test_get_or_create_idempotent(make_store):
    sc.assert_get_or_create_idempotent(make_store)

def test_different_key_different_session(make_store):
    sc.assert_different_key_different_session(make_store)

def test_save_persists_turn(make_store):
    sc.assert_save_persists_turn(make_store)

def test_dedup_tracks_message_ids(make_store):
    sc.assert_dedup_tracks_message_ids(make_store)
```

Three things to flag for the implementer:

1. **Import path**: `MemorySessionStore` lives at `claw_engine.engine.persistence.memory_store`,
   NOT `claw_engine.engine.persistence` (no top-level re-export).

2. **Factory shape**: the helper expects `Callable[[], SessionStore]` (factory). The fixture must
   return a callable, not a store instance directly.

3. **Sqlite :memory: behavior**: for `sqlite-sqlalchemy` and `sqlalchemy-postgres` the factory
   captures one engine/store and the closure returns the same instance on every call. This is
   intentional — within a single test, the helper's two `make_store()` calls (e.g. one before save
   and one after) must observe the same DB state. The existing `memory` + `sqlite` backends use
   `lambda: MemorySessionStore()` / `lambda: SqliteSessionStore(":memory:")` because
   `MemorySessionStore` and engine's `SqliteSessionStore(":memory:")` each happen to maintain state
   across instances differently (the in-process dict; the sqlite shared-cache for the engine
   variant). Implementer must verify each backend's `make_store` semantics actually let
   `assert_save_persists_turn` pass — the helper does NOT call `make_store()` twice but the
   factory contract still needs to be sound. If unclear, run the existing 12 cases before adding
   postgres to confirm baseline behaviour is preserved.

**Implementation note on fixture scoping**: `postgres_url` and `unique_table_prefix` live in
`tests/integration/conftest.py`. For `tests/contract/test_sessionstore_contract.py` to access
them, either:
- Move the fixtures to `tests/conftest.py` (top-level — available repo-wide)
- OR add `pytest_plugins = ["tests.integration.conftest"]` to `tests/contract/conftest.py` (create if not exists)

Implementer should pick the one matching existing pytest conventions in the repo. Document the
choice in the conftest docstring.

### 4.5 Schema parameter (Postgres-only)

```python
@pytest.mark.integration
def test_postgres_schema_argument_creates_tables_in_schema(
    postgres_url, unique_table_prefix
):
    """Verify P8e C1 fix on real Postgres: schema= actually puts tables in that schema."""
    schema_name = f"claw_test_{uuid.uuid4().hex[:6]}"
    
    # Create schema first (testcontainers gives us 'test' user with CREATEDB; CREATE SCHEMA is fine)
    with sqlalchemy.create_engine(postgres_url).begin() as conn:
        conn.execute(sqlalchemy.text(f"CREATE SCHEMA {schema_name}"))
    
    store = SQLAlchemySessionStore(postgres_url, schema=schema_name, table_prefix=unique_table_prefix)
    session = store.get_or_create(
        workspace_id="ws", channel="slack", external_thread_key="t",
        backend_name="codex", max_rounds=50,
    )
    
    # Verify table exists in the named schema
    with sqlalchemy.create_engine(postgres_url).begin() as conn:
        row = conn.execute(sqlalchemy.text(
            f"SELECT EXISTS (SELECT FROM information_schema.tables "
            f"WHERE table_schema = '{schema_name}' "
            f"AND table_name = '{unique_table_prefix}sessions')"
        )).scalar()
        assert row is True
```

---

## 5. Hard Contracts (preserved from P8e)

P9a does NOT touch any P8e contract. It only adds verification:

- HC-A: still hermetic construction (real Postgres URL accepted at construct, no IO until first call)
- HC-B: still create-only; real Postgres `metadata.create_all(checkfirst=True)` verified
- HC-C: credential redaction still holds; real Postgres DSN with embedded password rejected by validation (already covered by HC-C tests, but worth adding one integration test that uses the testcontainers-generated DSN which does NOT contain creds — sanity check)
- HC-D: now actually verified across 4 backends (memory + engine-sqlite + sqlalchemy-sqlite + sqlalchemy-postgres) when integration profile + Docker; MySQL adds a 5th in P9a.1

P9a additionally proves:
- **P1-#2 invariant** (the missing piece): `try/except IntegrityError` placement is correct on real Postgres; aborted-transaction footgun is closed.

Note: **HC-D scope in P9a** is "memory + engine-sqlite + sqlalchemy-sqlite + sqlalchemy-postgres"
= 4 backends running 4 assertions = 16 cases (when integration profile + Docker). MySQL would
add a 5th backend in P9a.1.

---

## 6. File Layout

**Corrected file names** (verified against current repo state):

```
tests/integration/
├── __init__.py                  # EXISTING
├── conftest.py                  # NEW: postgres_container, postgres_url, unique_table_prefix fixtures
├── README.md                    # NEW: how to run integration tests, Docker requirements, env vars
└── test_sqlalchemy_session_store_integration.py  # REWRITE: replace 3 Postgres stubs with real
                                  # impls; drop or keep MySQL stubs (mark skip "→ P9a.1")
                                  # Add: fixture sanity test (§3 end)
                                  # Add: schema= argument test (§4.5)

tests/contract/
├── sessionstore_contract.py     # EXISTING helper (assertion functions) — UNCHANGED
└── test_sessionstore_contract.py  # MODIFIED: parametrise add sqlalchemy-postgres backend
                                  # gated by integration marker; switch fixture-based store
                                  # resolution per §4.4

pyproject.toml additions:
  [project.optional-dependencies]
  integration = ["testcontainers[postgres]>=4.0"]   # postgres only; mysql defers to P9a.1
  
  (existing pytest markers already include "integration"; no change needed)
```

`conftest.py` soft cap: 150 lines (fixtures only, no logic).
`test_sqlalchemy_session_store_integration.py`: ~250 lines for 4-5 Postgres tests with full assertions.
P9a does NOT touch `tests/contract/sessionstore_contract.py` (the assertion helper) — only the
runner `test_sessionstore_contract.py` gets a new parametrize entry + fixture-based store resolution.

---

## 7. pyproject extras (locked)

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []
identity-casbin = ["casbin>=1.30"]
secrets-openbao = ["requests>=2.30"]
persistence-sqlalchemy = ["sqlalchemy>=2.0"]
persistence-mysql = ["sqlalchemy>=2.0", "pymysql>=1.1"]
persistence-postgres = ["sqlalchemy>=2.0", "psycopg[binary]>=3.1"]
integration = ["testcontainers[postgres]>=4.0"]  # NEW — postgres only; mysql defers to P9a.1
```

Installation for integration:
```bash
pip install -e ".[persistence-postgres,integration,dev]"
```

(Note: P9a does NOT add `persistence-mysql` to the integration extras since MySQL is deferred.
P9a.1 will both add `testcontainers[mysql]` and the small `connect_args` adapter extension.)

`testcontainers` itself pulls `docker` Python SDK transitively; user must have Docker daemon.

---

## 8. CI Story (deferred infra)

P9a does NOT set up CI automation. Document in `tests/integration/README.md`:

```
Running integration tests locally (Postgres only — MySQL deferred to P9a.1):

1. Ensure Docker daemon is running
2. Install extras: pip install -e ".[persistence-postgres,integration]"
3. Run: pytest -q -m integration tests/ -v
   (NOTE: `tests/` NOT `tests/integration/` — the postgres contract cases live in
   `tests/contract/test_sessionstore_contract.py` and would be missed by a `tests/integration/`-only invocation)
4. Expected: 9 integration-marked cases (5 in test_sqlalchemy_session_store_integration.py +
   4 postgres contract cases). Run in ~10s after first image pull.
   (concurrent race, duplicate idempotency, cross-restart durability,
   schema= argument, fixture sanity, + 4 cross-backend contract assertions)

Three explicit invocation forms:
- pytest -q                          → 519 baseline + 5 integration + 4 postgres contract = 528 (REQUIRES Docker; bare pytest does NOT auto-skip integration since pyproject has no addopts default filter)
- pytest -q -m "not integration"     → 519 baseline (default CI; no Docker needed)
- pytest -q -m integration tests/    → 5 integration + 4 postgres contract = 9 cases (note: `tests/` NOT `tests/integration/`, otherwise the contract-suite postgres entries are missed)

CI integration (future):
- GitHub Actions matrix job with services: postgres
- Or: cron-triggered nightly run with Docker-in-Docker
- Both deferred to a separate infra plan
```

---

## 9. Definition of Done

- [ ] `tests/integration/conftest.py` with 3 fixtures (postgres_container scope=session + postgres_url + unique_table_prefix); MySQL fixtures deferred to P9a.1
- [ ] `postgres_url` fixture returns credential-free DSN AND sets `PGUSER`/`PGPASSWORD` env vars via monkeypatch
- [ ] Skip-gracefully behavior when Docker unavailable
- [ ] 3 Postgres deferred stubs replaced with real implementations; MySQL stubs either removed or marked `pytest.skip("→ P9a.1")`
- [ ] 3 mandatory test categories present and passing on Postgres (concurrent race, duplicate idempotency, cross-restart durability)
- [ ] P1-#2 killer test passes on Postgres with deterministic IntegrityError-forcing barrier (proves the structural fix actually works on real DB)
- [ ] Fixture sanity test (§3 end) — verifies the fixture URL itself passes P8e validation
- [ ] Existing `tests/contract/test_sessionstore_contract.py` extended to add `sqlalchemy-postgres` backend gated by `pytest.mark.integration`; fixture-based store resolution via `request.getfixturevalue()`
- [ ] Postgres `schema=` argument verified on real Postgres (§4.5)
- [ ] `[integration]` extra in pyproject (testcontainers[postgres] only — no mysql)
- [ ] `tests/integration/README.md` documents how to run + Docker requirement + env vars
- [ ] Default CI (`pytest -q -m "not integration"`) unchanged — still 519 + 1 skipped (no new tests in default)
- [ ] Integration profile invocation correctly documented per §4.4 table (3 distinct pytest -m forms)
- [ ] Integration profile (`pytest -q -m integration tests/`, **scoped to `tests/` NOT `tests/integration/`**) all green when Docker available. Must include BOTH `tests/integration/test_sqlalchemy_session_store_integration.py` (5 cases) AND the postgres-marked entries in `tests/contract/test_sessionstore_contract.py` (4 cases). Total: 9 integration-marked cases under Docker.
- [ ] `git diff v1.1.0..HEAD -- claw_engine/engine/` → empty (engine still untouched)
- [ ] `git diff v1.1.0..HEAD -- claw_engine/adapters/` → empty (adapter code unchanged — P9a is test-only)
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 10. Locked Decisions (no confirm round needed)

1. **Scope: P8e + Postgres only**; MySQL → P9a.1; OpenBao → P9b. Each is a separate plan to keep
   credential/infra issues isolated (per user 2026-05-29).
2. **testcontainers library**, NOT manual Docker / docker-compose / docker-py
3. **postgres:16.4-alpine** image, pinned to specific minor (not floating `:16-alpine`)
4. **session-scoped container + per-test `table_prefix` isolation** for speed without flakiness
5. **Credential-free DSN via env vars**: fixture constructs URL with no userinfo and sets
   `PGUSER`/`PGPASSWORD` via monkeypatch. Relies on psycopg/libpq's documented env-var fallback.
   See §1.1 for why MySQL cannot use this path.
6. **Explicit driver form** (`postgresql+psycopg://`) per P8e P1-#1 allowlist; fixture constructs
   URL directly rather than using `testcontainers.PostgresContainer.get_connection_url()` which
   returns `postgresql+psycopg2://user:pass@...`
7. **Skip gracefully on missing Docker**; don't error
8. **Default CI does NOT run integration**; opt-in via `-m integration`. See §4.4 for the exact
   3 invocation forms and case counts.
9. **Engine + adapter code unchanged**; P9a is pure test + fixture + dependency addition
10. **No CI automation in P9a** — document manual invocation; CI infra is separate plan
11. **MariaDB / Oracle / MSSQL out of scope** — V1 ships postgres + mysql only
12. **`[integration]` pyproject extra** (not `[dev-integration]` or `[test-integration]`) — short;
    contains `testcontainers[postgres]` only (mysql extras defer with the rest of MySQL)
13. **Deterministic race test**: P1-#2 killer test uses SQLAlchemy `before_cursor_execute` event
    hook + barrier between SELECT and INSERT to force both threads to attempt INSERT simultaneously.
    Plain `threading.Barrier` before `get_or_create` is insufficient — could let first thread
    complete before second starts, missing the race. (§4.1)
14. **Fixture file location**: `postgres_url` and `unique_table_prefix` live in
    `tests/integration/conftest.py`; access from `tests/contract/test_sessionstore_contract.py`
    via either top-level `tests/conftest.py` re-export or `pytest_plugins` declaration.
    Implementer picks; documented in conftest docstring.

---

## 11. Acceptance Focus (user 2026-05-29, revised)

> Real Postgres contract suite green via testcontainers (MySQL deferred to P9a.1); specifically
> the 3 categories (concurrent race with deterministic event-hook barrier, duplicate idempotency,
> cross-restart durability) all pass on Postgres; P8e P1-#2 IntegrityError fix verified end-to-end
> on real Postgres (not just sqlite + source-inspection); engine unchanged; adapter code unchanged;
> default CI unchanged.

The single most important test is **§4.1 concurrent race** on Postgres — if the P1-#2 structural
fix is actually wrong in a way the source-inspection regression test couldn't catch, that test
will fail with a Postgres "current transaction is aborted" error message. That's the proof we
still owe from P8e.
