# Integration Tests

Integration tests spin up real database containers via
[testcontainers](https://testcontainers.com/). They are gated with
`@pytest.mark.integration` and **skipped in default CI**.

## Prerequisites

1. Docker daemon running (Docker Desktop or Docker Engine)
2. Install extras:
   ```bash
   pip install -e ".[persistence-postgres,integration]"
   ```

## Running integration tests

```bash
# Run all integration-marked tests (Postgres + postgres contract cases)
# NOTE: use `tests/` NOT `tests/integration/` — the postgres contract cases
# live in tests/contract/test_sessionstore_contract.py and would be missed
# by a `tests/integration/`-only invocation.
pytest -q -m integration tests/
```

Expected output (when Docker available, first run pulls image ~80MB):

```
9 passed, 3 skipped in ~10s
```

The 9 integration-marked cases that PASS are:
- 5 in `tests/integration/test_sqlalchemy_session_store_integration.py`
  1. `test_postgres_fixture_url_passes_p8e_validation`
  2. `test_concurrent_get_or_create_deterministically_triggers_integrity_error_on_postgres` (P1-#2 killer)
  3. `test_duplicate_mark_processed_does_not_break_subsequent_ops`
  4. `test_cross_restart_session_persists`
  5. `test_postgres_schema_argument_creates_tables_in_schema`
- 4 in `tests/contract/test_sessionstore_contract.py` (sqlalchemy-postgres backend)
  1. `test_get_or_create_idempotent[sqlalchemy-postgres]`
  2. `test_different_key_different_session[sqlalchemy-postgres]`
  3. `test_save_persists_turn[sqlalchemy-postgres]`
  4. `test_dedup_tracks_message_ids[sqlalchemy-postgres]`

Additionally, 3 MySQL stubs (`test_mysql_*`) are collected and unconditionally
`pytest.skip("-> P9a.1")`, bringing the total `pytest -m integration` collection
count to **12 (9 pass + 3 skip)**.

## Three invocation forms

| Command | Collected | Result | Docker needed |
|---|---|---|---|
| `pytest -q -m "not integration"` | 519 | 519 passed, 12 deselected | No |
| `pytest -q -m integration tests/` | 12 | 9 passed, 3 skipped, 519 deselected | Yes |
| `pytest -q` (no filter) | 531 | 528 passed, 3 skipped | Yes |

## Default CI

Default CI MUST use `-m "not integration"`:

```bash
pytest -q -m "not integration"
```

Bare `pytest -q` collects integration-marked tests and **fails without Docker**
because `pyproject.toml` does NOT set `addopts` to filter them out (by design —
see sub-plan §2.4).

## Skip behaviour

- If `testcontainers` is not installed: whole module is skipped via
  `pytest.importorskip("testcontainers")` — no error.
- If Docker daemon is not available: `postgres_container` fixture calls
  `pytest.skip("Docker daemon not available…")` — graceful skip, not failure.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CLAW_TC_TIMEOUT_SECONDS` | `60` | Container startup timeout in seconds |
| `PGUSER` | set by fixture | Postgres username (set automatically via monkeypatch) |
| `PGPASSWORD` | set by fixture | Postgres password (set automatically via monkeypatch) |

## Credential-free DSN (P8e HC-C compliance)

The `postgres_url` fixture returns a DSN with **no embedded credentials**:
```
postgresql+psycopg://localhost:54321/test
```
psycopg/libpq reads `PGUSER` and `PGPASSWORD` from environment variables at
connect time. This is required by P8e HC-C which rejects DSNs with embedded
`user:pass@`. See sub-plan §1.1 for full rationale.

## MySQL (deferred to P9a.1)

MySQL integration tests are deferred. The credential-free DSN approach used
for Postgres does not work for MySQL (`pymysql` does not read PGUSER/PGPASSWORD
equivalents). P9a.1 adds `connect_args` to `SQLAlchemySessionStore.__init__`
to unblock MySQL integration.

## CI automation (deferred)

GitHub Actions integration matrix or nightly Docker-in-Docker run is deferred
to a separate infra plan.
