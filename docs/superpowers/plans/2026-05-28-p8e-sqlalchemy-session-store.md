# P8e: SQLAlchemySessionStore Adapter — Sub-Plan

**Parent plan:** `/Users/lucas.xu/.claude/plans/cryptic-twirling-sonnet.md` § Tier-2 / P8e
**Baseline:** main @ `454e0c5` (after PR #4 P8d merge), tag `v1.0.0`
**Branch:** `feat/p8e-sqlalchemy-session-store`

---

## Why this sub-plan exists

P8e is structurally the simplest of the 5 adapters — no HTTP, no subprocess, no policy parsing.
But it shares two risk categories with P8d that earned 3+ review rounds there:

1. **Connection URL credentials** (DSN `postgresql://user:pass@host/db`) — same HC-D-family leak
   surface as OpenBao's endpoint.
2. **Input boundary validation** — adapter is a public class; anyone with a `SessionStore` reference
   can call `get_or_create(workspace_id=...)` with arbitrary input. Same C1 defense-in-depth lesson
   from P8d.

Plus two P8e-specific decisions worth locking:
- **SQLAlchemy ORM vs Core** — pick Core (lower-level matches Protocol dataclass shape; ORM is
  overkill)
- **Connection management** — lazy `Engine` creation on first call (NOT in `__init__`); single
  pooled `Engine` per adapter instance

Sub-plan is shorter than P8d (~300 lines vs 500) since no HTTP attack surface.

---

## 1. Lifecycle (3 phases, strict separation)

| Phase | Methods | Allowed | Forbidden |
|---|---|---|---|
| **Construction** | `__init__(url, *, schema=None, table_prefix="claw_")` | pure-string validation of DSN, schema name, table_prefix | DB connection, SQL execution, table creation |
| **First-use** | first call to any of `get_or_create / save / is_processed / mark_processed` | lazy `sqlalchemy.Engine` creation; `CREATE TABLE IF NOT EXISTS` for both tables (idempotent) | — |
| **Steady-state** | subsequent calls | SQL execution via the shared `Engine` | re-running CREATE TABLE on every call |

**Construction is hermetic — no DB IO** (HC-A). Pure-string validation only: DSN scheme check,
table_prefix regex check, schema name regex check. NO `sqlalchemy.create_engine` call (that opens
a connection lazily but builds pool state we want to defer).

**First call materializes**: lazy `_ensure_engine()` constructs the SQLAlchemy `Engine`, opens one
connection, runs `CREATE TABLE IF NOT EXISTS` for both `<prefix>sessions` and
`<prefix>processed_messages`. Subsequent calls reuse the same `Engine`.

---

## 2. Constructor Signature

```python
class SQLAlchemySessionStore(SessionStore):
    def __init__(
        self,
        url: str,                       # SQLAlchemy DSN: sqlite:///, mysql+pymysql://, postgresql+psycopg://
        *,
        schema: str | None = None,      # Postgres schema; ignored for sqlite/mysql; None = default
        table_prefix: str = "claw_",    # All tables prefixed; avoids collision with user tables
    ) -> None: ...
```

Validation in `__init__` (all pure-string, no DB):

- `url` must be non-empty; must start with `sqlite:`, `mysql:`, `mysql+pymysql:`, `postgresql:`,
  or `postgresql+psycopg:`. Other schemes (`mssql+`, `oracle+`, etc.) → `ValueError`. Adapter
  V1 only ships drivers for these three families.
- `url` must NOT contain embedded credentials in `username`/`password` fields (parsed via
  `sqlalchemy.engine.url.make_url`). DSNs with credentials → `ValueError`. **HC-C requirement.**
  Caller must use other auth (env vars consumed by driver, IAM tokens, etc.) or wrap DSN through
  `SecretProvider`.
- `url` must NOT contain query parameters that could carry secrets (e.g. `?password=foo`); if
  query is present, validate against the strict allowlist below. Unknown query keys → `ValueError`.

**DSN query allowlist (locked per user 2026-05-29)**:
```python
_ALLOWED_DSN_QUERY_KEYS = frozenset({
    # universal
    "connect_timeout",
    "charset",
    # postgres
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "application_name",
    # mysql / pymysql
    "ssl_ca",
    "ssl_cert",
    "ssl_key",
    "ssl_verify_cert",
    "ssl_verify_identity",
})
```
Principle: only connection security / encoding / timeout / client identity params. ANY key not in
this list → `ValueError`, even if it looks benign. Explicitly forbidden (would have been caught by
not-in-allowlist anyway, but flagged for clarity): `password`, `passwd`, `pwd`, `auth_token`,
`api_key`, `secret` — never allowed.
- `schema` (if not None) must match `[A-Za-z_][A-Za-z0-9_]+`; reject SQL keywords (use small
  static list: SELECT, FROM, WHERE, …).
- `table_prefix` must match `[A-Za-z_][A-Za-z0-9_]*` (note: empty `""` is allowed — disable prefix)
  and not exceed 32 chars.

Any violation → `ValueError`.

**Order in `__init__`**: validate ALL inputs BEFORE storing any `self._x = ...`. Failed
construction leaves no partial state (P8d HC-A lesson).

---

## 3. Connection Management (LOCKED)

**Single shared `sqlalchemy.Engine` per `SQLAlchemySessionStore` instance.** Created lazily on
first call to a Protocol method via `_ensure_engine()`. Reused for all subsequent calls.

Rationale:
- SQLAlchemy `Engine` is the connection pool. Per-call engines defeat pooling.
- `Engine` is thread-safe; multiple ChannelRunner threads can call the store concurrently.
- Lazy construction preserves HC-A (no IO at `__init__`).

Lifetime:
- Created on first call; reused.
- Adapter does NOT expose `close()`. SQLAlchemy `Engine` will be GC'd when the adapter is dropped;
  Engine's `__del__` closes pool. For prod with explicit shutdown, add a public `dispose()` method
  in a follow-up — out of scope V1.

Concurrency:
- `_ensure_engine` uses a single `threading.Lock` to prevent two threads racing into double engine
  creation. Once engine exists, the lock isn't held on read.
- Beyond engine creation, SQLAlchemy handles concurrency.

---

## 4. ORM vs Core (LOCKED — Core)

Use **SQLAlchemy 2.x Core API** (`sqlalchemy.Table`, `sqlalchemy.MetaData`, `sqlalchemy.insert`,
`sqlalchemy.select`, `sqlalchemy.update`). NOT the ORM (`sqlalchemy.orm.DeclarativeBase`, mapped
classes).

Reasons:
- Engine Protocol uses `Session` frozen dataclass — a separate ORM model would need redundant
  mapping
- Core API is leaner (~50 lines for both tables + 4 methods vs ~150 with ORM)
- No ORM session lifecycle to manage (no `session.commit()` / `session.flush()` nuances)
- Type safety is identical; both Core and ORM are typed in 2.x

Pattern:
```python
_metadata = sqlalchemy.MetaData()
_sessions_table = sqlalchemy.Table(
    f"{prefix}sessions",
    _metadata,
    sqlalchemy.Column("session_id", sqlalchemy.String(64), primary_key=True),
    sqlalchemy.Column("workspace_id", sqlalchemy.String(128), nullable=False),
    # ... rest of 9 fields
)

# In get_or_create:
with self._engine.begin() as conn:
    row = conn.execute(
        sqlalchemy.select(_sessions_table).where(
            _sessions_table.c.workspace_id == workspace_id,
            _sessions_table.c.channel == channel,
            _sessions_table.c.external_thread_key == external_thread_key,
        )
    ).one_or_none()
    if row is None:
        # insert
        ...
```

Schemas defined at module level (NOT inside `__init__`), constructed once per import. `table_prefix`
parameter requires per-instance table objects — handled by `_build_tables(prefix)` helper that
returns `(sessions_table, processed_messages_table)` against a fresh MetaData.

---

## 5. Schema (locked)

Two tables, both with prefix (`claw_` default):

### `<prefix>sessions`
| Column | Type | Constraints |
|---|---|---|
| `session_id` | `String(64)` | PRIMARY KEY |
| `workspace_id` | `String(128)` | NOT NULL |
| `channel` | `String(64)` | NOT NULL |
| `external_thread_key` | `String(256)` | NOT NULL |
| `backend_name` | `String(64)` | NOT NULL |
| `backend_thread_id` | `String(256)` | NULL |
| `round_count` | `Integer` | NOT NULL, DEFAULT 0 |
| `max_rounds` | `Integer` | NOT NULL, DEFAULT 50 |
| `last_active` | `Float` | NOT NULL, DEFAULT 0.0 |

Composite UNIQUE index on `(workspace_id, channel, external_thread_key)` — that's the
"natural key" engine uses to look up sessions. PRIMARY KEY is the synthetic UUID `session_id`.

### `<prefix>processed_messages`
| Column | Type | Constraints |
|---|---|---|
| `session_id` | `String(64)` | NOT NULL, FK → sessions(session_id) ON DELETE CASCADE |
| `message_id` | `String(256)` | NOT NULL |
| | | PRIMARY KEY (session_id, message_id) |

**Migration model**: `CREATE TABLE IF NOT EXISTS` for both tables, called from `_ensure_engine`
exactly once per process. No Alembic, no schema versioning. V1 ships v1 schema; future schema
changes go through a separate plan (likely Alembic-based).

**Schema/table naming for postgres**: when `schema` is set, tables go in that schema (`SET search_path`
or qualified names). For mysql/sqlite, `schema` is ignored.

---

## 6. Hard Contracts (locked)

### HC-A — Construction is hermetic (no DB IO)

`__init__` does pure-string validation only. NO `create_engine` call, NO connection, NO table
creation.

**Required tests:**
- `test_construction_does_no_db_io`: monkey-patch `sqlalchemy.create_engine` to raise; construction
  succeeds for all valid kwargs.
- `test_construction_validates_url_scheme`: `oracle+cx_oracle://...` → `ValueError`.
- `test_construction_rejects_url_with_credentials`: `postgresql://user:pass@host/db` → `ValueError`.
- `test_construction_rejects_url_with_unknown_query`: `postgresql://host/db?password=x` → `ValueError`.

### HC-B — Schema migration is idempotent + create-only

`_ensure_engine` runs `CREATE TABLE IF NOT EXISTS` for both tables. Never DROP, never ALTER. Safe
to call repeatedly (idempotent at any DB state where the tables already exist).

**Required tests:**
- `test_schema_migration_idempotent`: instantiate, call `get_or_create`, instantiate AGAIN against
  same sqlite file, call `get_or_create` — second call must not error.
- `test_schema_migration_never_drops`: inspect generated SQL via `sqlalchemy.event` or
  `_sessions_table.create(checkfirst=True)`; confirm only CREATE statements emitted, no DROP/ALTER.
  (Implementation: hook `Engine` events to record executed DDL strings; assert none contain `DROP`
  or `ALTER`.)

### HC-C — Connection URL credentials never leak via repr/str/exception

Same family as P8d HC-D. The adapter stores `self._url` (the string); `__repr__` must redact
the password component if any DSN parser accidentally accepts one.

**Validation order**: constructor REJECTS credentials in URL (per §2). But `__repr__` is the
belt-and-suspenders against:
- Future relaxation of constructor validation
- Caller bypassing validation by setting `_url` post-construction

**Required tests:**
- `test_repr_does_not_leak_url_password`: bypass construction validation (set `provider._url`
  directly to a URL with embedded password using sentinel `"PLAINTEXT-PW-DO-NOT-LEAK"`), then
  assert `repr(provider)` and `str(provider)` and any error containing `_url` do NOT contain the
  sentinel.
- `test_construction_rejection_message_does_not_leak_password`: pass DSN with embedded password to
  constructor; ValueError message must not echo the password.

### HC-D — Cross-backend Session round-trip equality

`Session` dataclass returned by `get_or_create / save round-trip` MUST be byte-identical across
sqlite / mysql / postgres backends. Same input, same output. This is the engine-side substitutability
guarantee.

**Required tests:**
- Existing engine `tests/contract/session_store_contract.py` already exists (or similar; the engine
  already has memory + sqlite contract running same suite). EXTEND it to add a `sqlalchemy-sqlite`
  parametrize case in default CI. mysql/postgres via testcontainers in `tests/integration/`.
- All contract suite assertions must pass identically across all backends.

---

## 7. Defense-in-Depth at Adapter Boundary

P8d added `_validate_workspace_id` etc. at adapter boundary. P8e's situation is different:
**SQLAlchemy's parameterized queries already prevent SQL injection completely.** Adding regex
validation on `workspace_id`, `channel`, etc. would be redundant for the SQL-injection vector.

**However**, we still validate at adapter boundary for these reasons:
- Catching invalid input early gives better error messages
- Defense in depth if SQLAlchemy ever has a CVE
- Consistency with P8d's adapter-boundary discipline

**Decision (locked per user 2026-05-29)**: validate at adapter boundary with **non-empty + length
cap matching the schema column** (§5). NO regex — engine's `_validate_workspace_id` regex is for
URL/filesystem paths and would wrongly reject legitimate `channel` thread keys (e.g.
`"slack:T01ABC#general/thread/123"` has colons, slashes, hashes).

| Field | Non-empty | Length cap (matches §5 schema) |
|---|---|---|
| `workspace_id` | ✅ | 128 |
| `channel` | ✅ | 64 |
| `external_thread_key` | ✅ | 256 |
| `backend_name` | ✅ | 64 |
| `session_id` | ✅ | 64 |
| `backend_thread_id` | nullable | 256 (when not None) |
| `message_id` | ✅ | 256 |

`table_prefix` (constructor input) IS strictly regex-validated per §2 because it's interpolated
into SQL DDL — same family as workspace_id but stricter (matches `[A-Za-z_][A-Za-z0-9_]*`, max 32
chars, no `.`/`..`).

Length violation → `ValueError` (caller error, not adapter-internal). Non-empty violation same.

---

## 8. File Layout

```
claw_engine/adapters/persistence/
├── __init__.py                  # empty (package stub)
└── sqlalchemy/
    ├── __init__.py              # exports SQLAlchemySessionStore, SessionStoreError
    ├── store.py                 # main class, ≤ 300 lines target (Core API is concise)
    ├── schema.py                # _build_tables(prefix) → (sessions, processed_messages) tuple
    └── errors.py                # SessionStoreError (adapter-local, e.g. for misconfigured DSN)

tests/adapters/
└── test_sqlalchemy_session_store.py  # invariants + HCs

tests/contract/
└── session_store_contract.py    # EXISTING — extend to include sqlalchemy-sqlite parametrize

tests/integration/
└── test_sqlalchemy_session_store_integration.py  # mysql + postgres via testcontainers, @pytest.mark.integration
```

`store.py` soft cap: 300 lines (higher than other adapters because SQLAlchemy Core has more
boilerplate). If grows beyond ~400, flag.

---

## 9. pyproject extras (locked per user 2026-05-29 — short names)

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []
identity-casbin = ["casbin>=1.30"]
secrets-openbao = ["requests>=2.30"]
persistence-sqlalchemy = ["sqlalchemy>=2.0"]              # base + sqlite (stdlib sqlite3, no driver dep)
persistence-mysql = ["sqlalchemy>=2.0", "pymysql>=1.1"]   # adds mysql driver
persistence-postgres = ["sqlalchemy>=2.0", "psycopg[binary]>=3.1"]  # adds postgres driver
```

Rationale: caller installs only what they need. A user running sqlite-only doesn't pull `pymysql`
or `psycopg`; a postgres user doesn't pull `pymysql`. The split matches the parent plan's
"adapter opt-in" principle exactly.

Test gating: `pytest.importorskip("sqlalchemy")` at module top of `test_sqlalchemy_session_store.py`.
mysql/postgres integration tests use `pytest.importorskip("pymysql")` / `pytest.importorskip("psycopg")`
respectively.

---

## 10. Test Invariants

⭐ = acceptance-critical.

### Construction & validation

1. ⭐ HC-A: `test_construction_does_no_db_io` — monkey-patch `create_engine` to bomb; constructor succeeds
2. `test_construction_validates_url_scheme` — reject `oracle://`, `mssql://`, etc.
3. ⭐ HC-C: `test_construction_rejects_url_with_credentials` — `postgresql://user:pass@host` → ValueError
4. `test_construction_rejects_url_with_unknown_query` — only `sslmode`, `ssl_ca`, `sslrootcert` allowed
5. `test_construction_validates_schema_name` — reject SQL keywords, bad chars
6. `test_construction_validates_table_prefix` — reject bad chars, length > 32
7. `test_construction_accepts_valid_inputs` — parametrise across sqlite/mysql/postgres URLs + valid schema + valid prefix

### Lazy connection

8. `test_first_call_creates_engine` — assert `_engine is None` before first call, then exists after
9. `test_engine_reused_across_calls` — assert same `Engine` instance across multiple calls
10. `test_concurrent_first_call_does_not_double_create` — two threads call `get_or_create` simultaneously; only one `Engine` created (verify via mock)

### Schema migration

11. ⭐ HC-B: `test_schema_migration_idempotent` — call twice with fresh adapter against same sqlite file; both succeed
12. ⭐ HC-B: `test_schema_migration_never_drops_or_alters` — record DDL via Engine events; assert no DROP/ALTER

### CRUD via Protocol

13. `test_get_or_create_creates_new_session` — first call creates; returns Session with all fields
14. `test_get_or_create_returns_existing_session` — second call with same natural key returns SAME session_id
15. `test_save_updates_existing_session` — save with bumped round_count persists
16. `test_save_does_not_create_new_session` — engine contract: save() of unknown session_id is no-op (or error per Protocol — match memory_store behavior; check existing impl)
17. `test_is_processed_returns_false_initially` — fresh session has no processed messages
18. `test_mark_processed_then_is_processed_true` — mark, then check
19. `test_mark_processed_idempotent` — calling twice doesn't error

### Token / credential leakage

20. ⭐ HC-C: `test_repr_does_not_leak_url_password` — bypass validation, inject password sentinel, scan repr/str/error messages
21. ⭐ HC-C: `test_construction_rejection_message_does_not_leak_password` — DSN with creds → ValueError message excludes password

### Cross-backend contract

22. ⭐ HC-D: `tests/contract/session_store_contract.py` extended to parametrise across `memory + engine-sqlite + sqlalchemy-sqlite` (default CI). Confirm all assertions pass identically.
23. Optional integration: mysql + postgres via testcontainers, `@pytest.mark.integration`

### Protocol compatibility

24. `test_isinstance_session_store` — `isinstance(adapter, SessionStore) is True` via runtime_checkable

### Sub-table FK behavior

25. `test_processed_messages_cascade_on_session_delete` — only relevant if/when a delete method exists (V1 has none); skip or document as future
26. `test_table_prefix_isolation` — two adapters with different prefixes share a sqlite DB; neither sees the other's sessions

---

## 11. Integration Tests (optional V1)

`tests/integration/test_sqlalchemy_session_store_integration.py`, marked `@pytest.mark.integration`.

Setup: `testcontainers` Python lib launches mysql + postgres containers; adapter runs against each.

3 focused tests per backend (mysql + postgres = 6 total):
1. End-to-end CRUD: create, save, mark_processed, is_processed all work
2. Cross-restart durability: stop adapter, restart with same URL, retrieve session
3. Schema migration on fresh container: starts empty, adapter creates tables

If too heavy for one PR, defer per parent plan §9. **Fake/sqlite tests are mandatory; integration
tests are nice-to-have V1.**

---

## 12. Definition of Done

- [ ] All 24 invariant tests in §10 passing (excluding optional #23, #25)
- [ ] All 4 Hard Contracts (HC-A/B/C/D) have at least one ⭐ test
- [ ] `pytest -q` → 416 + new tests, all green
- [ ] `pytest tests/purity -q` → 2/2 (engine still clean)
- [ ] `git diff v1.0.0..HEAD -- claw_engine/engine/` → empty
- [ ] `grep -rn "from claw_engine.adapters" claw_engine/engine/` → empty
- [ ] `grep -rn "sqlalchemy" claw_engine/engine/` → empty (or only existing pre-P8 references)
- [ ] `[persistence-sqlalchemy]` extras (3 of them) added; default install has no SQLAlchemy
- [ ] Existing `session_store_contract.py` parametrised cleanly to include sqlalchemy-sqlite
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 13. Out of Scope (defer to later)

- **Async variant** (`AsyncSessionStore` Protocol + `sqlalchemy[asyncio]` impl) — separate plan
- **Alembic migrations** — V1 single-schema, future schema changes need Alembic plan
- **`dispose()` method** — explicit engine shutdown; SQLAlchemy GC handles V1
- **Connection pool tuning** — V1 uses SQLAlchemy defaults; expose params later if perf matters
- **Read replicas / write-through cache** — V1 single primary connection
- **Custom serializers** for `Session` fields — V1 maps 1:1 to columns
- **Soft delete / archival** — V1 has no delete; sessions live forever (engine doesn't delete either)
- **Oracle / SQL Server drivers** — V1 ships sqlite + mysql + postgres only

---

## 14. Locked Decisions (no confirm round needed)

1. **Sync only** (no `sqlalchemy[asyncio]` extras)
2. **SQLAlchemy 2.x Core API** (no ORM)
3. **Drivers**: stdlib `sqlite3`, `pymysql` for mysql, `psycopg` for postgres
4. **Lazy `Engine` creation** on first Protocol call; single shared `Engine` per adapter; `threading.Lock`
   protecting double-creation; no explicit `dispose()` in V1
5. **Schema**: 2 tables (`<prefix>sessions`, `<prefix>processed_messages`), composite UNIQUE index on
   natural key, FK with CASCADE
6. **Migration**: `CREATE TABLE IF NOT EXISTS` only, idempotent, no DROP/ALTER, no Alembic
7. **Construction**: hermetic (HC-A); DSN credential rejection (HC-C); schema/table_prefix validation
8. **`table_prefix`**: default `"claw_"`; empty string allowed; max 32 chars
9. **Connection URL allowlist queries**: `sslmode`, `ssl_ca`, `sslrootcert` only
10. **Cross-backend contract**: extend existing `session_store_contract.py` to include sqlalchemy-sqlite;
    mysql/postgres via testcontainers integration profile
11. **No SQL injection-style adapter-boundary regex on workspace_id/channel/etc.** (locked
    2026-05-29) — SQLAlchemy parameterized queries handle SQL injection. Adapter validates only
    non-empty + length cap matching §5 schema columns. Engine's `_validate_workspace_id` regex is
    for URL/filesystem use and would wrongly reject legitimate channel thread keys (colons,
    slashes, hashes in IDs like `"slack:T01ABC#general/thread/123"`).
13. **DSN query allowlist** (locked 2026-05-29): `{connect_timeout, charset, sslmode,
    sslrootcert, sslcert, sslkey, application_name, ssl_ca, ssl_cert, ssl_key, ssl_verify_cert,
    ssl_verify_identity}`. Any key not in this set → `ValueError`. Explicitly forbidden by name
    (caught by allowlist anyway): `password`, `passwd`, `pwd`, `auth_token`, `api_key`, `secret`.
14. **Extras naming** (locked 2026-05-29): `persistence-sqlalchemy` (base + sqlite via stdlib),
    `persistence-mysql` (adds pymysql), `persistence-postgres` (adds psycopg). Short names; no
    redundant `-sqlalchemy-` infix.
12. **`SessionStoreError`** adapter-local exception (in `errors.py`) for misconfigured DSN /
    schema mismatch at first use. Engine Protocol's `SessionStore` has no exception types defined;
    adapter is free.

---

## 15. Acceptance Focus (single sentence)

> Construction is DB-IO-free; lazy single-Engine per adapter; CREATE-only idempotent migration;
> DSN credentials rejected at construction AND never leak via repr/str/exception; cross-backend
> Session round-trip byte-identical (sqlite + mysql + postgres pass same contract suite).

Any deviation requires a new plan.
