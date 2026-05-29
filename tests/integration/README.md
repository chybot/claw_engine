# Integration Tests

Integration tests spin up real database/service containers via
[testcontainers](https://testcontainers.com/). They are gated with
`@pytest.mark.integration` and **skipped in default CI**.

## Prerequisites

1. Docker daemon running (Docker Desktop or Docker Engine)
2. Install extras:
   ```bash
   # Postgres integration tests
   pip install -e ".[persistence-postgres,integration]"

   # OpenBao integration tests
   pip install -e ".[secrets-openbao,integration]"

   # Both together
   pip install -e ".[persistence-postgres,secrets-openbao,integration]"
   ```

## Running integration tests

```bash
# Run all integration-marked tests (Postgres + OpenBao + postgres contract cases)
# NOTE: use `tests/` NOT `tests/integration/` — the postgres contract cases
# live in tests/contract/test_sessionstore_contract.py and would be missed
# by a `tests/integration/`-only invocation.
pytest -q -m integration tests/
```

Expected output (when Docker available, first run pulls images ~100MB):

```
16 passed, 3 skipped in ~15s
```

The 16 integration-marked cases that PASS are:
- 7 in `tests/integration/test_openbao_secret_integration.py` (P9b)
  1. `test_get_secrets_returns_seeded_kv_v2_data` (happy path: seed + read)
  2. `test_get_secrets_returns_empty_on_unseeded_workspace` (HC-B: 404 → `{}`)
  3. `test_get_secrets_raises_on_bad_token` (HC-C-auth: bad token → `SecretProviderError(stage="auth")`)
  4. `test_get_secrets_raises_on_unreachable_endpoint` (HC-C-network: closed port → `SecretProviderError(stage="network")`)
  5. `test_real_openbao_auth_error_does_not_leak_bad_token` (HC-D: bad sentinel absent from auth error)
  6. `test_real_network_error_does_not_leak_good_token` (HC-D: good sentinel absent from network error)
  7. `test_get_secrets_rejects_invalid_workspace_id_before_http` (HC-A: workspace_id validation before HTTP)
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
count to **19 (16 pass + 3 skip)**.

## Three invocation forms

| Command | Collected | Result | Docker needed |
|---|---|---|---|
| `pytest -q -m "not integration"` | 519 | 519 passed, 19 deselected | No |
| `pytest -q -m integration tests/` | 19 | 16 passed, 3 skipped, 519 deselected | Yes |
| `pytest -q` (no filter) | 538 | 535 passed, 3 skipped | Yes |

## Default CI

Default CI MUST use `-m "not integration"`:

```bash
pytest -q -m "not integration"
```

Bare `pytest -q` collects integration-marked tests; without Docker, those cases
skip gracefully via fixture gating. Default CI still filters them out because
`pyproject.toml` does NOT set `addopts` to do that automatically (by design —
see sub-plan §2.4).

## Skip behaviour

- If `testcontainers` is not installed: `openbao_container` / `postgres_container`
  fixtures call `pytest.importorskip("testcontainers")` → clean Skipped (not error).
- If `requests` is not installed: `test_openbao_secret_integration.py` module-top
  `pytest.importorskip("requests")` → whole module skipped cleanly.
- If Docker daemon is not available: container fixtures call
  `pytest.skip("Docker daemon not available…")` — graceful skip, not failure.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CLAW_TC_TIMEOUT_SECONDS` | `60` | Container startup timeout in seconds (shared by all containers) |
| `DOCKER_HOST` | (unset) | Override Docker socket path (needed for Colima / Rancher Desktop) |
| `PGUSER` | set by fixture | Postgres username (set automatically via monkeypatch) |
| `PGPASSWORD` | set by fixture | Postgres password (set automatically via monkeypatch) |

**Colima users**: testcontainers uses `docker.from_env()` which defaults to
`/var/run/docker.sock`. If you use Colima with a non-standard socket (e.g.
`~/.colima/codex-ego/docker.sock`), set `DOCKER_HOST`:

```bash
export DOCKER_HOST=unix:///Users/<you>/.colima/<instance>/docker.sock
pytest -q -m integration tests/
```

## Credential-free DSN (P8e HC-C compliance)

The `postgres_url` fixture returns a DSN with **no embedded credentials**:
```
postgresql+psycopg://localhost:54321/test
```
psycopg/libpq reads `PGUSER` and `PGPASSWORD` from environment variables at
connect time. This is required by P8e HC-C which rejects DSNs with embedded
`user:pass@`. See sub-plan §1.1 for full rationale.

## OpenBao integration (P9b)

OpenBao tests use `openbao/openbao:2.0.0` in dev mode with a sentinel root token.

### BAO_DEV_* env vars and sentinel design

The container is started with:
- `BAO_DEV_ROOT_TOKEN_ID=OPENBAO-TEST-ROOT-DO-NOT-LEAK` — the canonical OpenBao
  env var (not the HashiCorp Vault alias `VAULT_DEV_ROOT_TOKEN_ID`)
- `BAO_DEV_LISTEN_ADDRESS=0.0.0.0:8200` — bind address for container port mapping

Two sentinel tokens exist for HC-D (token leakage) tests:
- **Good sentinel** (`OPENBAO-TEST-ROOT-DO-NOT-LEAK`): the dev root token, used
  in happy-path and network-error tests. HC-D network-error test scans for this.
- **Bad sentinel** (`OPENBAO-BAD-TOKEN-DO-NOT-LEAK`): an invalid token used only
  in HC-D auth-error tests. HC-D auth-error test scans for this.

Each HC-D test scans the sentinel **actually transmitted** in its failing request —
not the unrelated sentinel. Mixing them up silently false-greens (the untransmitted
sentinel can't appear in the error regardless of leakage).

### Parse-error path (deferred)

A malformed-200 integration test is intentionally absent. Triggering it from real
OpenBao would require a fake-proxy layer, contradicting the "real backend" intent.
P8d's fake-HTTP unit tests in `tests/adapters/test_openbao_secret.py` cover that
path comprehensively.

## MySQL (deferred to P9a.1)

MySQL integration tests are deferred. The credential-free DSN approach used
for Postgres does not work for MySQL (`pymysql` does not read PGUSER/PGPASSWORD
equivalents). P9a.1 adds `connect_args` to `SQLAlchemySessionStore.__init__`
to unblock MySQL integration.

## CI automation (deferred)

GitHub Actions integration matrix or nightly Docker-in-Docker run is deferred
to a separate infra plan.
