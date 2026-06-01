# P9a.1: MySQL Integration + `connect_args` Adapter Extension

**Parent context:** Final piece of v1.2.0. Builds on P8e (SQLAlchemySessionStore adapter), P9a
(testcontainers infra + Postgres integration), P9b (OpenBao integration as testcontainers second
backend).
**Baseline:** main @ `84fd149` (P9b merged), tag `v1.1.1`.
**Branch:** `feat/p9a1-mysql-connect-args`.
**Tag target:** `v1.2.0` "integration surfaces complete".

---

## Why this sub-plan exists

P9a deferred MySQL integration to a follow-up because P8e's HC-C rejects DSNs with embedded
credentials, and MySQL (unlike Postgres) has no env-var fallback for credential-free DSN. The
locked path is to **add a `connect_args` kwarg** to `SQLAlchemySessionStore.__init__` — a small
additive adapter extension that opens a SEPARATE credential channel from the URL, preserving
HC-C while enabling MySQL.

This is the **only** non-test code change between v1.1.0 (P8 shipped) and v1.2.0. Everything
else in v1.0.0→v1.2.0 was either pure test additions (P9a, P9b) or adapter implementations
landing under existing Protocols. P9a.1 is the smallest possible adapter extension consistent
with shipping MySQL: one kwarg, one storage attr, one passthrough at engine-creation time.

---

## 1. Scope (locked)

### In scope (P9a.1)

- Add `connect_args: Mapping[str, Any] | None = None` keyword-only kwarg to
  `SQLAlchemySessionStore.__init__`
- Store defensively-copied dict on `self._connect_args`
- Pass to `sqlalchemy.create_engine(..., connect_args=...)` ONLY when non-None
- Update HC-C to cover TWO layers (DSN reject + connect_args no-leak)
- Add MySQL container fixture to `tests/conftest.py` (mirror Postgres pattern)
- Activate 3 MySQL stubs in `tests/integration/test_sqlalchemy_session_store_integration.py`
  with real implementations
- Update `tests/integration/README.md` invocation counts
- Update pyproject `[integration]` extras to include `testcontainers[mysql]`

### Out of scope (still deferred or never-in-scope)

- **Aborted-transaction killer test for MySQL** — InnoDB doesn't have this semantics; trying to
  invent one would be a brittle DB-behavior test, not a contract test
- **MySQL-specific dialect features** (NDB cluster, partitioning, ON UPDATE timestamps) — out
  of scope; if a user needs them, they layer on top of the standard SessionStore
- **MariaDB compatibility** — V1.x decision (we ship MySQL via pymysql; MariaDB best-effort)
- **`save_secrets` / write methods** for the SessionStore Protocol — unchanged from P8e
- **Async variant** — separate plan
- **`connect_args` validation** beyond "dict-ish or None" — caller is responsible for what they
  pass (SQLAlchemy validates at engine-creation time)
- **Other backends** — Oracle, MSSQL, etc. remain out of P9a.1
- **Modifying P8e sub-plan** — P8e is the shipped baseline; P9a.1 is a self-contained delta on
  top, not a P8e revision

---

## 2. Adapter Delta (THE only non-test change)

**File:** `claw_engine/adapters/persistence/sqlalchemy/store.py`

Three localized edits:

### 2.1 Signature

Existing P8e class declaration is **`class SQLAlchemySessionStore:`** (NOT `(SessionStore)` —
the Protocol is satisfied structurally via `@runtime_checkable`, no inheritance). P9a.1 leaves
the class declaration untouched; only `__init__` signature gains the new kwarg:

```python
class SQLAlchemySessionStore:   # unchanged from P8e — do NOT add (SessionStore) base
    def __init__(
        self,
        url: str,
        *,
        schema: str | None = None,
        table_prefix: str = "claw_",
        connect_args: Mapping[str, Any] | None = None,   # NEW
    ) -> None: ...
```

`Mapping[str, Any]` (not `dict`) — accepts read-only mappings too; defensive copy converts to
dict internally. **Keyword-only** (already enforced by `*` separator).

Default `None` — NOT `{}` (avoid mutable default, avoid changing default-path behavior from
P8e). When `None`, the adapter does NOT pass `connect_args=` to SQLAlchemy at all; SQLAlchemy
falls back to its own defaults (empty dict).

### 2.2 Storage (defensive copy)

```python
self._connect_args: dict[str, Any] | None = (
    dict(connect_args) if connect_args is not None else None
)
```

Defensive copy via `dict(...)` so that:
- Caller can't mutate the adapter's internal state after construction
- Accepts any `Mapping`, normalizes to `dict`
- Original `None` preserved as `None` (not `{}`)

### 2.3 Passthrough at engine creation

```python
# In _ensure_engine, where create_engine is called:
engine_kwargs: dict[str, Any] = {}
if self._connect_args is not None:
    engine_kwargs["connect_args"] = self._connect_args
engine = sqlalchemy.create_engine(self._url, **engine_kwargs)
```

NOT `connect_args=self._connect_args or {}` — that would always pass an empty dict in the
default path, changing SQLAlchemy's behavior subtly (some dialects treat presence-of-empty-dict
differently from absence). Conditional passthrough preserves the P8e baseline path.

### 2.4 What HC-A/B/D are unchanged

- **HC-A** (construction hermetic): `dict(connect_args)` is pure-Python; no IO. Still hermetic.
- **HC-B** (schema migration CREATE-only): unchanged — `connect_args` affects connection setup,
  not DDL.
- **HC-D** (cross-backend Session round-trip): unchanged — Session dataclass shape doesn't
  depend on `connect_args`.

Only **HC-C** (credentials) gets extended — see §3.

---

## 3. HC-C Two-Layer Verification

P8e HC-C said: "Connection URL credentials never leak via repr/str/exception". P9a.1 keeps that
intact AND adds a parallel layer for `connect_args`.

### 3.1 Layer 1: DSN credential rejection (REINFORCED)

Existing tests already cover `postgresql+psycopg://user:pass@host/db` → ValueError. Add MySQL
explicitly:

```python
def test_construction_rejects_mysql_dsn_with_credentials() -> None:
    """HC-C layer 1: even though MySQL is the use case for connect_args,
    embedded URL credentials are STILL rejected. connect_args is the
    intended channel; URL is not."""
    with pytest.raises(ValueError, match="credentials"):
        SQLAlchemySessionStore(
            "mysql+pymysql://user:secret@host/db",
            connect_args={"user": "user", "password": "secret"},  # the intended channel
        )
```

The constructor's existing URL-parsing validator handles this; no adapter code change needed for
layer 1 — only the test coverage extends.

### 3.2 Layer 2: connect_args credential no-leak (NEW)

`connect_args` is the legitimate credential channel for MySQL (`{"user": "...", "password": "..."}`).
The adapter MUST NOT echo these credentials in any default-output surface:

- `repr(store)` — must not contain `connect_args` values
- `str(store)` — same
- `SessionStoreError.__str__` / `__repr__` — must not contain values
- `SessionStoreError.args` — same

**`__cause__` chain explicitly NOT in scope** — `raise ... from exc` preserves the original
SQLAlchemy/driver exception, which may itself surface connection metadata. Constraining that
would force the adapter to deeply parse + sanitize every driver exception, which is fragile and
not the user's leak surface. P8d HC-D made the same carve-out (P8d tests scan `str(exc)` and
`exc.args` but not `__cause__`).

**Sentinel pattern**: use `PLAINTEXT-CONNECT-ARGS-DO-NOT-LEAK` as the password value in tests,
scan adapter repr + error messages for the literal string.

```python
_CA_SENTINEL = "PLAINTEXT-CONNECT-ARGS-DO-NOT-LEAK"

def test_repr_does_not_leak_connect_args_credentials() -> None:
    store = SQLAlchemySessionStore(
        "mysql+pymysql://host/db",
        connect_args={"user": "alice", "password": _CA_SENTINEL},
    )
    # Layer 2a: no credential VALUE in repr/str
    assert _CA_SENTINEL not in repr(store)
    assert _CA_SENTINEL not in str(store)
    # Layer 2b: no "connect_args" KEY/LABEL in repr either — lock §3.3 decision
    # so a future "connect_args=<redacted>" addition fails this test even though
    # the sentinel scan would still pass. Forces explicit re-review of repr surface.
    assert "connect_args" not in repr(store)
    assert "connect_args" not in str(store)


def test_init_failure_does_not_leak_connect_args_credentials() -> None:
    """HC-C layer 2: if create_engine raises (e.g. unreachable host), the
    wrapped SessionStoreError(stage='init') must NOT echo connect_args."""
    # Use unreachable host to force init failure
    store = SQLAlchemySessionStore(
        "mysql+pymysql://127.0.0.1:1/db",
        connect_args={"user": "alice", "password": _CA_SENTINEL, "connect_timeout": 1},
    )
    with pytest.raises(SessionStoreError) as exc_info:
        store.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                            backend_name="fake", max_rounds=10)
    _assert_no_connect_args_leak(exc_info.value, _CA_SENTINEL)
```

### 3.3 What the adapter must NOT do for layer 2

The adapter doesn't need any new code to satisfy layer 2 IF the existing `__repr__` doesn't
already print `self._connect_args`. Verify current P8e `__repr__` — it should be limited to
URL (with password-stripped via SQLAlchemy's `render_as_string(hide_password=True)`), schema,
table_prefix. **Do NOT add `connect_args=...` to repr**, NOT EVEN as `connect_args=<redacted>`.

The layer-2 test (§3.2 above + §5.1 #6) enforces this in two ways:
1. Sentinel-value scan: literal credential value must not appear
2. **Label scan**: the literal string `"connect_args"` must not appear in repr/str

The second assertion exists specifically to block the obvious future regression: someone adds
`connect_args=<redacted>` thinking it's safe; sentinel-value scan still passes (the value is
redacted); but the design intent (connect_args is a SEPARATE channel, not advertised in repr)
gets violated silently. Belt-and-braces — forces explicit re-review of repr surface before any
future maintainer can add connect_args to repr in any form.

---

## 4. MySQL Container Fixture

`tests/conftest.py` gains 2 fixtures + reuses `unique_table_prefix`:

```python
@pytest.fixture(scope="session")
def mysql_container() -> Iterator["MySqlContainer"]:
    """Start a MySQL 8 container... skips if testcontainers/Docker unavailable.

    Mirrors postgres_container pattern:
      - context-manager protocol (with container as c: yield c) for cleanup safety
      - pytest.importorskip("testcontainers") inside body, not at module top
      - Graceful skip on Docker unavailable
    """
    pytest.importorskip(
        "testcontainers",
        reason="testcontainers not installed (pip install -e .[integration])",
    )
    from testcontainers.mysql import MySqlContainer

    try:
        container = MySqlContainer(
            image=_MYSQL_IMAGE,   # "mysql:8.0.36" pinned minor
            username=_MYSQL_TEST_USER,
            password=_MYSQL_TEST_PASSWORD,
            dbname=_MYSQL_TEST_DBNAME,
        )
        with container as c:
            yield c
    except Exception as exc:
        pytest.skip(f"Docker daemon not available or MySQL container failed: {exc}")


@pytest.fixture
def mysql_dsn_and_connect_args(mysql_container) -> tuple[str, dict[str, Any]]:
    """Returns credential-free DSN + connect_args for MySQL.

    P8e HC-C rejects user:pass@ in URL. testcontainers' get_connection_url()
    returns the user:pass@ form, so we construct our own credential-free URL
    and pass credentials via connect_args (the legitimate channel for MySQL,
    which has no PGUSER/PGPASSWORD-style env-var fallback like Postgres)."""
    host = mysql_container.get_container_host_ip()
    port = mysql_container.get_exposed_port(3306)
    dsn = f"mysql+pymysql://{host}:{port}/{_MYSQL_TEST_DBNAME}"
    connect_args = {
        "user": _MYSQL_TEST_USER,
        "password": _MYSQL_TEST_PASSWORD,
    }
    return dsn, connect_args


_MYSQL_IMAGE = "mysql:8.0.36"     # pinned minor, per P9a lesson — verify published at impl time
_MYSQL_TEST_USER = "claw_test"
_MYSQL_TEST_PASSWORD = "MYSQL-TEST-PW-DO-NOT-LEAK-AT-CONTAINER-LEVEL"
_MYSQL_TEST_DBNAME = "claw_test"
```

The fixture returns a **tuple** `(dsn, connect_args)` for two reasons:
1. The test signature makes the two-channel design explicit (caller passes both to constructor)
2. Tests using `connect_args` for HC-C verification can introspect them separately

**MySQL test password is also sentinel-shaped** — defense-in-depth for HC-D-style scans if the
container's password ever leaks via testcontainers' own logging.

### 4.1 Why `MySqlContainer` is fine here (vs. P9a Postgres complication)

Unlike Postgres where `PostgresContainer(driver=...)` mismatch caused P9a P1-#3 silent
false-green, `MySqlContainer` defaults work with pymysql out of the box (pymysql is one of the
common defaults). Still, **explicitly pass username/password/dbname to the constructor** — don't
rely on `MySqlContainer`'s defaults (`test/test`) being stable across testcontainers versions.

### 4.2 fixture path file location

Per P9a precedent, fixtures live in `tests/conftest.py` (top-level, auto-discovered by
subpackages). Add OpenBao + MySQL alongside Postgres. Conftest grows; if it exceeds ~350 lines,
flag for extraction in a polish PR (still well under that today).

---

## 5. Test Plan

### 5.1 Adapter unit tests (in `tests/adapters/test_sqlalchemy_session_store.py`)

Default CI; no Docker. Cover the adapter delta:

1. `test_connect_args_default_none_does_not_pass_to_create_engine` — monkey-patch
   `sqlalchemy.create_engine` to record kwargs; construct adapter without `connect_args`; trigger
   `_ensure_engine`; assert `connect_args` NOT in the recorded kwargs (preserves P8e baseline
   path)
2. `test_connect_args_non_none_passes_dict_to_create_engine` — same monkey-patch; construct
   with `connect_args={"k": "v"}`; assert recorded kwargs include `connect_args={"k": "v"}`
3. `test_connect_args_is_defensively_copied` — pass a dict, mutate it post-construction, assert
   `self._connect_args` unchanged (via `store._connect_args`)
4. `test_connect_args_accepts_mapping_not_just_dict` — pass `types.MappingProxyType({"k": "v"})`
   (read-only view), construct succeeds, internal storage is a dict copy
5. **HC-C layer 1**: `test_construction_rejects_mysql_dsn_with_credentials` (parametrize already-existing
   credential-rejection test to include `mysql+pymysql://u:p@host` form)
6. **HC-C layer 2 repr**: `test_repr_does_not_leak_connect_args_credentials` — TWO assertions:
   (a) `_CA_SENTINEL` not in repr/str (value scan), (b) literal string `"connect_args"` not in
   repr/str (label scan, locks §3.3 design decision that connect_args is never advertised in
   repr — not even as `connect_args=<redacted>`)
7. **HC-C layer 2 init failure**: `test_init_failure_does_not_leak_connect_args_credentials`
   (sentinel scan on `SessionStoreError`)

Total adapter test additions: ~7. All run on default CI.

### 5.2 MySQL integration tests (in `tests/integration/test_sqlalchemy_session_store_integration.py`)

Replace 3 existing MySQL `pytest.skip("→ P9a.1")` stubs with real implementations. All marked
`@pytest.mark.integration`.

**MySQL has no Postgres P1-#2 equivalent** (no aborted-transaction state). The 3 mandatory
categories per user's lock are:

1. **`test_mysql_end_to_end_crud_with_connect_args_auth`**
   - Uses `mysql_dsn_and_connect_args` fixture
   - Constructs store with both DSN + connect_args
   - Exercises full Protocol contract: `get_or_create` creates fresh, `get_or_create` again with
     same natural key returns same session_id (idempotency), `save(s.with_turn(...))` persists,
     `mark_processed("msg-1")` then `is_processed("msg-1") is True`, `mark_processed("msg-1")`
     twice → no error (duplicate no-op idempotency)
   - This is the "MySQL HC-D equivalent" — proves Session round-trip on real backend

2. **`test_mysql_fresh_schema_create_only`**
   - Two adapter instances against same DB + same table_prefix
   - First instance triggers schema creation; second instance must succeed without DROP/ALTER
     (idempotent CREATE-only — same HC-B verification as Postgres §4.5)

3. **`test_mysql_cross_restart_durability`**
   - Phase 1: create + save with turn + mark_processed; dispose store1
   - Phase 2: new store against same container + prefix; retrieve same session_id, observe
     persisted backend_thread_id, round_count, and processed message

Optional 4th if implementer has time (NOT mandatory per locked spec):

4. **`test_mysql_repr_does_not_leak_connect_args_credentials`** — duplicate of §5.1 #6 but on
   real backend, with the testcontainers-generated password as the sentinel target. If skipped,
   the unit test §5.1 #6 carries sufficient coverage.

**Engine + adapter purity for integration tests**: same as P9a — these tests don't change the
adapter; they exercise the new `connect_args` kwarg. The adapter delta IS the v1.2.0 change.

### 5.3 Cross-backend contract suite extension

Extend `tests/contract/test_sessionstore_contract.py`'s `_BACKENDS` list to include
`sqlalchemy-mysql` (parametrized with `pytest.mark.integration`):

```python
_BACKENDS = [
    "memory",
    "sqlite",
    pytest.param("sqlalchemy-sqlite", marks=pytest.mark.skipif(not _SA_AVAILABLE, reason="...")),
    pytest.param("sqlalchemy-postgres", marks=[
        pytest.mark.integration,
        pytest.mark.skipif(not _SA_AVAILABLE, reason="..."),
    ]),
    pytest.param("sqlalchemy-mysql", marks=[                   # NEW
        pytest.mark.integration,
        pytest.mark.skipif(not _SA_AVAILABLE, reason="..."),
    ]),
]
```

`make_store` fixture grows a `"sqlalchemy-mysql"` branch:

```python
if backend == "sqlalchemy-mysql":
    dsn, connect_args = request.getfixturevalue("mysql_dsn_and_connect_args")
    prefix = request.getfixturevalue("unique_table_prefix")
    store = SQLAlchemySessionStore(dsn, table_prefix=prefix, connect_args=connect_args)
    return lambda: store
```

Total contract suite size becomes 5 backends × 4 assertions = 20 cases (4 baseline + 16
integration); `pytest -m integration` collects 8 (postgres 4 + mysql 4).

---

## 6. File Layout

```
claw_engine/adapters/persistence/sqlalchemy/store.py    # MODIFIED — connect_args kwarg (3 small edits per §2)
tests/adapters/test_sqlalchemy_session_store.py         # MODIFIED — +7 adapter tests (§5.1)
tests/conftest.py                                       # MODIFIED — +2 MySQL fixtures + 4 module-level constants
tests/integration/test_sqlalchemy_session_store_integration.py   # MODIFIED — replace 3 MySQL stubs (§5.2)
tests/contract/test_sessionstore_contract.py            # MODIFIED — +1 backend in _BACKENDS (§5.3)
tests/integration/README.md                             # MODIFIED — counts + MySQL section
pyproject.toml                                          # MODIFIED — [integration] extras add mysql; possibly [persistence-mysql] tweaks
```

No new files. Estimated diff size:
- `store.py`: ~10 lines added (signature param + storage + passthrough conditional)
- `test_sqlalchemy_session_store.py`: ~150 lines added (7 tests)
- `conftest.py`: ~80 lines added (2 fixtures + constants)
- integration test file: ~150 lines (3 stub replacements at ~50 lines each)
- contract runner: ~10 lines (new backend in list + make_store branch)
- README: ~20 lines (MySQL section + count updates)
- pyproject: 1 line

`store.py` soft cap: stays under 320 lines (current 312 + ~10 = ~322; verify with implementer).

---

## 7. pyproject Extras Update

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []
identity-casbin = ["casbin>=1.30"]
secrets-openbao = ["requests>=2.30"]
persistence-sqlalchemy = ["sqlalchemy>=2.0"]
persistence-mysql = ["sqlalchemy>=2.0", "pymysql>=1.1"]
persistence-postgres = ["sqlalchemy>=2.0", "psycopg[binary]>=3.1"]
integration = ["testcontainers[postgres,vault,mysql]>=4.0"]   # MODIFIED: add mysql
```

The `persistence-mysql` extra was already declared in P8e (P8e P1-#1 fix locked
`mysql+pymysql://` scheme + extras). P9a.1 only adds `mysql` to the `[integration]` testcontainers
extras for the new fixture.

---

## 8. Install Matrix Verification (4 states per P9a/P9b lesson)

Critical: P9a P1-#3 silent-false-green lesson still applies. Verify 4 install states explicitly:

| State | Command | Expected |
|---|---|---|
| Clean `.[dev]` (no SA, no mysql, no integration) | `pytest -q -m "not integration"` | baseline preserved, MySQL adapter tests skip via importorskip |
| Partial: SA only (`.[persistence-sqlalchemy,dev]`) | `pytest -q -m integration tests/` | MySQL contract case skips cleanly (no mysql_container fixture skip chain triggers) |
| Full Postgres only (`.[persistence-postgres,integration,dev]`) | `pytest -q -m integration tests/` | Postgres integration runs (Docker); MySQL test SKIPS (no `pymysql` driver) — verify graceful skip, not error |
| Full MySQL (`.[persistence-mysql,integration,dev]`) | `pytest -q -m integration tests/ -v` | MySQL integration tests actually run (proof-of-execution check) |

**Critical proof-of-execution check**: temporarily inject `assert False` into the MySQL CRUD
test, run, confirm FAILED (not SKIPPED). Revert, confirm PASSED. Same drill as P9a + P9b.

Default CI count preservation: `pytest -q -m "not integration"` MUST be `519 baseline + 7 new
adapter tests = 526` (or whatever the actual current default count is — verify at impl time)
with the integration cases deselected count growing to 23. The 3 existing MySQL stubs were already
integration-marked; P9a.1 activates those and adds 4 new MySQL contract cases.

---

## 9. CI Story (deferred infra, same as P9a/P9b)

Update `tests/integration/README.md` with new counts:

```
With full integration extras + Docker:
- pytest -q -m integration tests/   → 9 Postgres + 7 OpenBao + 7 MySQL = 23 collected, all pass
```

CI automation (GitHub Actions matrix with `services: postgres, mysql, openbao`) remains deferred
to a separate infra plan.

---

## 10. Definition of Done

- [ ] `connect_args: Mapping[str, Any] | None = None` keyword-only kwarg on
      `SQLAlchemySessionStore.__init__`
- [ ] Defensive copy via `dict(connect_args) if connect_args is not None else None`
- [ ] Conditional passthrough — adapter does NOT pass `connect_args=` to `create_engine` when
      None (preserves P8e baseline)
- [ ] 7 new adapter unit tests cover §5.1
- [ ] HC-C layer 1 still rejects MySQL DSN with credentials (new test + existing infrastructure)
- [ ] HC-C layer 2 enforced by 2 sentinel-scan tests (repr + init-failure exception)
- [ ] `mysql_container` + `mysql_dsn_and_connect_args` fixtures in `tests/conftest.py`,
      mirroring `postgres_container` pattern (context-manager cleanup per P9b I1 lesson)
- [ ] 3 MySQL integration test stubs replaced with real implementations covering CRUD/idempotency,
      schema CREATE-only, cross-restart durability
- [ ] Contract suite (`tests/contract/test_sessionstore_contract.py`) extended to 5 backends
- [ ] `[integration]` extras updated to `testcontainers[postgres,vault,mysql]>=4.0`
- [ ] `tests/integration/README.md` updated with new counts + MySQL section
- [ ] Install matrix verified across 4 states (clean / SA-only / Postgres+integration / MySQL+integration)
- [ ] Proof-of-execution check: `assert False` in MySQL CRUD test → FAILED → revert → PASSED
- [ ] Default CI count preserved (519 + 7 = 526 baseline expected; verify actual)
- [ ] Engine purity gate still passes
- [ ] All 4 P8e Hard Contracts still pass (run `pytest tests/adapters/test_sqlalchemy_session_store.py`)
- [ ] All P9a Postgres integration still passes (run `pytest -m integration tests/`)
- [ ] All P9b OpenBao integration still passes
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 11. Locked Decisions (no confirm round needed)

User confirmed all 4 lean defaults + refinements 2026-05-29:

1. **`connect_args: Mapping[str, Any] | None = None`** keyword-only — `Mapping` (not `dict`) for
   flexibility; `None` default (not `{}`) to avoid mutable default + preserve P8e baseline path
2. **Defensive copy** via `dict(connect_args) if connect_args is not None else None` — caller
   can't mutate adapter internals post-construction
3. **Conditional passthrough** in `_ensure_engine` — only pass `connect_args=` to `create_engine`
   when non-None; SQLAlchemy default path unchanged when `connect_args=None`
4. **HC-C two-layer verification**:
   - Layer 1: DSN credential reject (existing test, add MySQL coverage)
   - Layer 2: connect_args no-leak via `repr` + `SessionStoreError.__str__/__repr__/args`
   - `__cause__` deliberately NOT in scope (would over-constrain SQLAlchemy/driver internals)
5. **No MySQL aborted-tx killer** — InnoDB doesn't have this semantics; would be brittle DB
   behavior test, not a contract test. The 3 mandatory MySQL integration tests are CRUD/idempotency,
   schema CREATE-only, cross-restart durability.
6. **Self-contained sub-plan** — P8e sub-plan NOT modified; P9a.1 stands alone as the documented
   adapter delta + MySQL verification
7. **Sentinel pattern for connect_args**: `PLAINTEXT-CONNECT-ARGS-DO-NOT-LEAK` — distinct from
   P8d/P9b OpenBao sentinels; no cross-contamination risk
8. **Adapter delta is the ONLY non-test code change between v1.1.0 and v1.2.0** — preserves the
   "v1.X.Y bumps for adapter extensions, v1.X.Z bumps for test-only verifications" pattern
9. **MySQL container fixture mirrors Postgres pattern** — context-manager cleanup (P9b I1
   lesson); explicit username/password/dbname (not relying on testcontainers defaults)
10. **`mysql:8.0.36` pinned minor** — implementer verifies tag is published at impl time; bumps
    explicitly if needed (no floating `:8.0` tag)
11. **`mysql_dsn_and_connect_args` returns tuple `(dsn, dict)`** — explicitly two-channel; test
    signature makes design intent visible
12. **Install matrix 4 states** verified (clean / SA-only / Postgres+integration / MySQL+integration)
    + proof-of-execution check (P9a P1-#3 lesson reapplied)

---

## 12. Acceptance Focus (per user style)

> Adapter gains exactly one keyword-only `connect_args` kwarg with defensive copy + conditional
> passthrough; HC-A/B/D unchanged, HC-C extended to TWO layers (DSN reject + connect_args
> no-leak); MySQL integration via testcontainers with credential-free DSN + connect_args auth;
> 3 mandatory MySQL tests cover CRUD/idempotency + schema CREATE-only + cross-restart durability;
> default CI count preserved; engine zero diff vs v1.1.1; install matrix 4 states verified with
> proof-of-execution check.

The single most important integration test is **§5.2 #1 end-to-end CRUD with connect_args auth**
— if this passes, it proves the new credential channel works on a real MySQL backend through
the full SessionStore Protocol. Everything else (HC-C layer 2, contract suite extension,
cross-restart durability) is reinforcement.
