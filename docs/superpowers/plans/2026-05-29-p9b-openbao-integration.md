# P9b: OpenBao Integration Tests via testcontainers

**Parent context:** Follow-up to P8d (OpenBaoSecretProvider) + P9a (testcontainers infra).
**Baseline:** main @ `d73d41b` (P9a merged, tag `v1.1.1`).
**Branch:** `feat/p9b-openbao-integration`.
**Tag target:** `v1.2.0` (together with P9a.1 MySQL).

---

## Why this sub-plan exists

P8d (PR #4) shipped the OpenBao adapter with comprehensive fake-HTTP tests covering 4 Hard
Contracts and 71 unit tests. But like P8e before P9a, **real backend behavior was never
exercised**. P9b closes that loop using real OpenBao containers, reusing the testcontainers infra
P9a built.

The lesson from P9a P1-#3 (psycopg driver mismatch → silent false-green) is the operative concern
here: fake HTTP tests can pass while a real OpenBao deployment fails for reasons fake tests don't
model — readiness probe race conditions, real TLS handshake failures, KV v2 API quirks, Vault-fork
compatibility gaps. P9b verifies these.

**Scope strictly P9b** — no MySQL (P9a.1), no adapter code changes, no engine touch.

---

## 1. Scope (locked)

### In scope (P9b)

- OpenBao container fixture in `tests/conftest.py` mirroring P9a's `postgres_container` pattern
- Activate 3+ deferred OpenBao stubs in `tests/integration/` (or create the file if P8d didn't leave stubs)
- 6 mandatory test categories (§4): happy-path seed-and-read, HC-B 404, HC-C-auth, HC-C-network, HC-D token leakage on real errors, workspace_id boundary
- Update `tests/integration/README.md` to document OpenBao integration alongside Postgres
- Verify on the 3-state install matrix (clean `.[dev]` / partial / full) — same defense-in-depth as P9a

### Out of scope (deferred)

- **MySQL integration** → P9a.1 (separate plan; adds `connect_args` adapter kwarg)
- **`hvac` Python client integration** — P8d explicitly rejected hvac for the adapter; P9b reuses
  the same decision. Test fixtures use `requests` directly (already a dep via `[secrets-openbao]`).
- **Vault (HashiCorp) compatibility tests** — OpenBao is a Vault fork; we test OpenBao only. If a
  user runs against Vault, their burden to verify.
- **OpenBao TLS / mTLS** — V1 ships HTTP-only fixture; TLS testing is a separate plan
- **OpenBao auth methods beyond pre-issued root token** (AppRole, OIDC, JWT) — out of scope V1
- **Adapter changes of any kind** — P9b is pure test + fixture + dependency addition (no
  `connect_args`, no extras rename, no engine touch)
- **CI automation** — same deferral as P9a

---

## 2. Container Choice (locked)

### 2.1 Image

**`openbao/openbao:2.0.0`** (or current minor — verify the implementer-picked tag is published
at start of implementation). Reasons:
- Official OpenBao image
- Stable 2.x branch
- Pinned to specific minor for reproducibility (per P9a lesson `postgres:16-alpine` was wrong)

Implementer: if 2.0.0 isn't current, bump to current minor explicitly; do NOT use floating tag.

### 2.2 testcontainers wrapper — VaultContainer with explicit OpenBao env vars + fallback

testcontainers ships a `VaultContainer` (in `testcontainers.vault`) targeting HashiCorp Vault.
OpenBao is a Vault fork; the API + KV v2 endpoints are compatible, but **OpenBao's dev-mode env
vars are `BAO_*`-prefixed**, not `VAULT_*`. Per Docker Hub docs, OpenBao recognizes
`BAO_DEV_ROOT_TOKEN_ID` and `BAO_DEV_LISTEN_ADDRESS`; some versions also accept the
`VAULT_DEV_ROOT_TOKEN_ID` alias for compatibility, but we **must not** rely on aliases.

**Primary path** — use `VaultContainer` with explicit OpenBao env vars + image override:

```python
from testcontainers.vault import VaultContainer

container = (
    VaultContainer(image="openbao/openbao:2.0.0")
    .with_env("BAO_DEV_ROOT_TOKEN_ID", _TEST_TOKEN_SENTINEL)
    .with_env("BAO_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
)
```

The `VaultContainer.__init__(root_token=...)` kwarg sets `VAULT_DEV_ROOT_TOKEN_ID` under the hood
— that may or may not be honored by OpenBao depending on version. `.with_env("BAO_DEV_ROOT_TOKEN_ID", ...)`
is the authoritative form. Set both is fine but redundant.

**Fallback path** — if `VaultContainer`'s healthcheck (designed for HashiCorp Vault's `/v1/sys/health`)
doesn't accept OpenBao's response shape, drop to generic `DockerContainer` with explicit healthcheck:

```python
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

container = (
    DockerContainer("openbao/openbao:2.0.0")
    .with_env("BAO_DEV_ROOT_TOKEN_ID", _TEST_TOKEN_SENTINEL)
    .with_env("BAO_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
    .with_exposed_ports(8200)
)
container.start()
wait_for_logs(container, "core: post-unseal setup complete", timeout=_TC_TIMEOUT)
# Or HTTP retry loop on /v1/sys/health
```

Implementer: try primary path first; if healthcheck times out / errors, switch to fallback and
note in fixture docstring. Both paths require the same `BAO_DEV_*` env vars.

Add `testcontainers[vault]` to the `[integration]` extras so `VaultContainer` is importable.

### 2.3 Dev mode + readiness

OpenBao dev mode boots with an in-memory storage backend, a single unsealed instance, and a
caller-specified root token. This is the standard test pattern.

Readiness probe: GET `{endpoint}/v1/sys/health` until HTTP 200 (unsealed/active/leader). Use
testcontainers' built-in `wait_for_logs` for the "Vault server started" / "OpenBao server started"
log line, OR a manual HTTP retry loop with `_TC_TIMEOUT` honored (P9a wired this via
`testcontainers_config.max_tries`).

### 2.4 Skip behavior

Same as P9a: `pytest.importorskip("testcontainers")` inside the fixture body; on Docker missing,
graceful `pytest.skip(...)`. No silent false-green. **Always pass any required driver/client
kwargs explicitly to the container constructor** (P9a P1-#3 lesson — testcontainers' default
client may differ from what `[secrets-openbao]` installs).

For OpenBao: we use `requests` directly (already in `[secrets-openbao]` extras), no separate
"driver" mismatch risk. Verify this in implementation.

---

## 3. Fixture Design

`tests/conftest.py` gains **4 fixtures + 1 fixture helper** (alongside existing Postgres ones):

1. `openbao_container` — session-scoped, starts dev-mode OpenBao
2. `openbao_endpoint` — function-scoped, credential-free URL
3. `openbao_token` — function-scoped, the GOOD root sentinel
4. `openbao_bad_token` — function-scoped, the BAD sentinel for HC-D auth tests
5. `seed_secret` — helper fixture returning a closure that POSTs KV v2 secrets

Code:

```python
_TEST_TOKEN_SENTINEL = "OPENBAO-TEST-ROOT-DO-NOT-LEAK"
_TEST_BAD_TOKEN_SENTINEL = "OPENBAO-BAD-TOKEN-DO-NOT-LEAK"


@pytest.fixture(scope="session")
def openbao_container() -> Iterator["VaultContainer"]:
    """Start an OpenBao container in dev mode... skips if testcontainers/Docker unavailable.

    Skips cleanly when:
      - testcontainers not installed → importorskip → clean Skipped (not error)
      - Docker daemon unavailable → fixture except → pytest.skip with informative message
    """
    pytest.importorskip(
        "testcontainers",
        reason="testcontainers not installed (pip install -e .[integration])",
    )
    from testcontainers.vault import VaultContainer
    
    try:
        container = (
            VaultContainer(image="openbao/openbao:2.0.0")
            .with_env("BAO_DEV_ROOT_TOKEN_ID", _TEST_TOKEN_SENTINEL)
            .with_env("BAO_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
        )
        container.start()
    except Exception as exc:
        # If VaultContainer healthcheck timed out (OpenBao /v1/sys/health response shape may
        # differ across versions), see §2.2 fallback: switch to generic DockerContainer.
        pytest.skip(f"Docker daemon not available or OpenBao container failed: {exc}")
    
    yield container
    container.stop()


@pytest.fixture
def openbao_endpoint(openbao_container) -> str:
    """Returns endpoint URL like 'http://localhost:54321' — caller passes token separately
    (HC-C: credentials never in URL)."""
    host = openbao_container.get_container_host_ip()
    port = openbao_container.get_exposed_port(8200)
    return f"http://{host}:{port}"


@pytest.fixture
def openbao_token() -> str:
    """The dev root token. Same sentinel-like string used by openbao_container fixture
    so HC-D leakage tests can scan for it across error paths."""
    return _TEST_TOKEN_SENTINEL


@pytest.fixture
def openbao_bad_token() -> str:
    """A second sentinel-shaped token, used by HC-D tests that intentionally trigger auth
    failures. The bad token is what gets SENT in the failing request; if it appears in any
    error string/repr/args, that's a leak. (The good root token isn't relevant for those
    paths since it was never transmitted.)"""
    return _TEST_BAD_TOKEN_SENTINEL
```

Both sentinels are shaped distinctly enough to grep across logs/CI output without false
positives. The two-token design is the fix for the obvious-looking trap where the HC-D scan
checks the wrong sentinel (see §4.5 below).

### Helper fixture for seeding secrets

```python
@pytest.fixture
def seed_secret(openbao_endpoint, openbao_token):
    """Returns a helper to seed a KV v2 secret at a path for the current test.
    
    Usage:
        seed_secret("claw/workspaces/ws-1", {"OPENAI_KEY": "sk-test"})
    """
    import requests
    
    def _seed(path: str, data: dict[str, str]) -> None:
        url = f"{openbao_endpoint}/v1/secret/data/{path}"
        resp = requests.post(
            url,
            headers={"X-Vault-Token": openbao_token},
            json={"data": data},
            timeout=10,
        )
        resp.raise_for_status()
    
    return _seed
```

Use `requests` (already a dep) — no `hvac`.

---

## 4. Test Categories (mandatory)

`tests/integration/test_openbao_secret_integration.py` (NEW or REWRITE existing P8d stubs).
All marked `@pytest.mark.integration`.

**Two-level importorskip pattern** (P9a final lesson):
- **Module-top**: `pytest.importorskip("requests")` only — gates the hard dep without which
  the test file can't even define its sentinel constants
- **Fixture-body** (in `tests/conftest.py`): `pytest.importorskip("testcontainers")` — gates the
  optional container infra so that partial installs (requests yes, testcontainers no) skip
  cleanly rather than fail at collection

This split mirrors P9a — verified working pattern. Module top does NOT importorskip testcontainers
because the test file's own logic only uses `requests` directly; the container is wholly behind
the fixture.

### 4.1 Happy path: seed and read

```python
@pytest.mark.integration
def test_get_secrets_returns_seeded_kv_v2_data(openbao_endpoint, openbao_token, seed_secret):
    """End-to-end: seed → read → assert exact mapping."""
    seed_secret("claw/workspaces/ws-1", {"OPENAI_KEY": "sk-test", "PORT": "8080"})
    provider = OpenBaoSecretProvider(
        endpoint=openbao_endpoint,
        token=openbao_token,
    )
    result = provider.get_secrets("ws-1")
    assert result == {"OPENAI_KEY": "sk-test", "PORT": "8080"}
```

### 4.2 HC-B verification: 404 → `{}`

```python
@pytest.mark.integration
def test_get_secrets_returns_empty_on_unseeded_workspace(openbao_endpoint, openbao_token):
    """HC-B: workspace with no seeded secrets returns {}, NOT raise."""
    provider = OpenBaoSecretProvider(endpoint=openbao_endpoint, token=openbao_token)
    result = provider.get_secrets("ws-not-seeded")
    assert result == {}
    assert isinstance(result, dict)  # not None, not falsy-but-non-dict
```

### 4.3 HC-C-401 (bad token)

```python
@pytest.mark.integration
def test_get_secrets_raises_on_bad_token(openbao_endpoint):
    """HC-C: 401 → SecretProviderError(stage='auth'). Bad token bypasses our HC-A construction
    rejection because token format is not validated by adapter."""
    bad_token = "INVALID-BUT-WELL-FORMED-TOKEN"
    provider = OpenBaoSecretProvider(endpoint=openbao_endpoint, token=bad_token)
    
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")
    assert exc_info.value.stage == "auth"
    assert exc_info.value.status in (401, 403)
```

### 4.4 HC-C-network (container stopped)

This is the trickiest — we need to point the provider at a non-existent endpoint OR stop the
container mid-test. Stopping mid-test impacts session-scoped fixture; cleanest is pointing at
a closed port:

```python
@pytest.mark.integration
def test_get_secrets_raises_on_unreachable_endpoint(openbao_token):
    """HC-C: network error → SecretProviderError(stage='network').
    Uses a closed port — adapter must distinguish unreachable from auth/parse failures."""
    provider = OpenBaoSecretProvider(
        endpoint="http://127.0.0.1:1",  # unassigned port; refused immediately
        token=openbao_token,
        timeout_connect=2.0,  # fast fail
    )
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")
    assert exc_info.value.stage == "network"
```

### 4.5 HC-D: real-error token leakage (auth + network only — parse path deferred)

The most important integration test for HC-D. Real OpenBao errors are richer than fake-server
errors; if any path includes the token in error metadata (e.g. via requests library's
auto-stringification of `PreparedRequest.headers`), HC-D fails.

**Critical**: scan for the sentinel that was actually TRANSMITTED in the failing request, not
the unrelated good-token sentinel. The bad-token scenario sends `_TEST_BAD_TOKEN_SENTINEL`; the
good-token scenario doesn't fail at auth, so it's covered by the network-error path which sends
the good `_TEST_TOKEN_SENTINEL`. Two tests, two sentinels — explicit which one each scans for.

```python
@pytest.mark.integration
def test_real_openbao_auth_error_does_not_leak_bad_token(openbao_endpoint, openbao_bad_token):
    """HC-D: real auth failure (401/403) must NOT echo the bad token in error str/repr/args.
    
    Scans for openbao_bad_token (= 'OPENBAO-BAD-TOKEN-DO-NOT-LEAK') — the token that was actually
    transmitted in the failing request. The good root token isn't relevant because it was never
    transmitted on this path."""
    provider = OpenBaoSecretProvider(endpoint=openbao_endpoint, token=openbao_bad_token)
    
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")
    assert exc_info.value.stage == "auth"
    _assert_no_token_leak(exc_info.value, openbao_bad_token)


@pytest.mark.integration
def test_real_network_error_does_not_leak_good_token(openbao_token):
    """HC-D: real network error (connection refused) must NOT echo the good token in error
    str/repr/args. The good token was being transmitted attempt on the failing request, so
    this scans for openbao_token (= 'OPENBAO-TEST-ROOT-DO-NOT-LEAK')."""
    provider = OpenBaoSecretProvider(
        endpoint="http://127.0.0.1:1",  # closed port
        token=openbao_token,
        timeout_connect=2.0,
    )
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")
    assert exc_info.value.stage == "network"
    _assert_no_token_leak(exc_info.value, openbao_token)


def _assert_no_token_leak(exc: SecretProviderError, sentinel: str) -> None:
    """Reused across HC-D integration tests — matches the unit-test sentinel pattern.
    Caller passes the sentinel that was actually transmitted in the failing request."""
    assert sentinel not in str(exc), f"token leaked in str(exc): {exc}"
    assert sentinel not in repr(exc), f"token leaked in repr(exc): {exc!r}"
    for arg in exc.args:
        assert sentinel not in str(arg), f"token leaked in exc.args: {arg!r}"
```

**Parse-error path deferred from mandatory** — triggering a malformed-200 response from real
OpenBao would require proxying through a fake server, which contradicts "real backend" intent.
Parse error coverage stays with P8d's fake HTTP unit tests (which can craft arbitrary malformed
responses cleanly). If a future user-reported bug surfaces a real-OpenBao parse path that leaks
token, that triggers a separate PR — not P9b's mandatory scope.

### 4.6 workspace_id boundary on real backend

```python
@pytest.mark.integration
def test_get_secrets_rejects_invalid_workspace_id_before_http(openbao_endpoint, openbao_token, monkeypatch):
    """HC-A / C1 from P8d: workspace_id validation runs BEFORE any HTTP request.
    Validates the structural fix applies on real backend too (sanity)."""
    provider = OpenBaoSecretProvider(endpoint=openbao_endpoint, token=openbao_token)
    
    def bomb(*a, **kw):
        raise AssertionError("HTTP attempted despite invalid workspace_id")
    monkeypatch.setattr("requests.get", bomb)
    
    with pytest.raises(ValueError):
        provider.get_secrets("../escape")
```

### 4.7 Cross-restart "durability" (sanity — secrets are external state, not adapter state)

OpenBao dev mode loses data on restart. **Skip cross-restart durability for V1** — secrets
persistence is OpenBao's concern, not adapter's. Document the omission in test file.

---

## 5. No Adapter Code Diff (hard rule)

P9b is pure test + fixture + dependency addition:
- `git diff v1.1.1..HEAD -- claw_engine/engine/` → empty
- `git diff v1.1.1..HEAD -- claw_engine/adapters/` → empty
- All 4 P8d Hard Contracts (HC-A/B/C/D) still pass on the SAME adapter code

If integration testing surfaces a real adapter bug, that triggers a SEPARATE patch PR before P9b
merge. P9b itself ships zero adapter changes.

---

## 6. File Layout

```
tests/conftest.py                   # MODIFIED: add 4 OpenBao fixtures (openbao_container session-scoped,
                                    # openbao_endpoint + openbao_token + openbao_bad_token function-scoped)
                                    # + seed_secret helper fixture, alongside existing Postgres fixtures

tests/integration/test_openbao_secret_integration.py   # NEW (or REWRITE if P8d left stubs)
                                                       # ~250 lines for 6 mandatory tests (§4)

tests/integration/README.md         # MODIFIED: document OpenBao alongside Postgres invocations
                                    # update case counts (P9a's 9 + P9b's 6 = 15 integration cases)

pyproject.toml additions:
  [project.optional-dependencies]
  integration = [
      "testcontainers[postgres,vault]>=4.0",   # adds vault extras for OpenBao
  ]
```

`tests/conftest.py` soft cap: 250 lines (currently ~180; +70 for OpenBao fixtures).
Integration test file: ~250 lines for 6 mandatory tests with full assertions (no 7th — parse-path
deferred per §4.5).

---

## 7. pyproject extras update

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []
identity-casbin = ["casbin>=1.30"]
secrets-openbao = ["requests>=2.30"]
persistence-sqlalchemy = ["sqlalchemy>=2.0"]
persistence-mysql = ["sqlalchemy>=2.0", "pymysql>=1.1"]
persistence-postgres = ["sqlalchemy>=2.0", "psycopg[binary]>=3.1"]
integration = ["testcontainers[postgres,vault]>=4.0"]   # MODIFIED: add vault
```

Test gating: **module top of integration test file uses `pytest.importorskip("requests")` ONLY**
(the test file imports requests directly). `pytest.importorskip("testcontainers")` happens inside
the `openbao_container` fixture body in `tests/conftest.py`, NOT at module top — see §4 two-level
pattern (verified working in P9a). `requests` is already required by `[secrets-openbao]` so the
module-top importorskip serves only the case where someone installs `[integration]` without
`[secrets-openbao]` — informative skip rather than ImportError.

---

## 8. Install Matrix Verification (per P9a P1-#1/2/3 lesson)

Critical: verify all 3 install states. P9a's P1-#3 (driver mismatch silent false-green) is the
operative warning — verify integration tests ACTUALLY run, not just collect-and-skip.

| State | Command | Expected |
|---|---|---|
| Clean `.[dev]` (no requests, no testcontainers) | `pytest -q -m "not integration"` | module skipped at collection, baseline passes |
| Partial (`.[secrets-openbao,dev]`, no testcontainers) | `pytest -q -m integration tests/` | OpenBao integration tests skip cleanly via fixture-body `importorskip("testcontainers")` |
| Full (`.[secrets-openbao,integration,dev]` + Docker) | `pytest -q -m integration tests/ -v` | OpenBao tests actually run (not silent-skip); **exactly 6 cases pass** (no 7th — parse path deferred per §4.5) |

**Critical proof-of-execution check** (P9a P1-#3 lesson): temporarily inject `assert False` into
one OpenBao test, run, confirm FAILED. Revert, confirm PASSED. If the test silently SKIPS without
showing FAIL/PASS, there's a fixture-side issue masking actual execution.

---

## 9. CI Story (deferred infra)

Same as P9a: P9b does NOT set up CI automation. Update `tests/integration/README.md` to document
the new invocation count (15 integration cases when both Postgres + OpenBao Docker available):

```
With full integration extras + Docker:
- pytest -q -m integration tests/   → 9 Postgres + 6 OpenBao + 3 MySQL stubs = 18 collected (15 pass, 3 skip)
```

Note: P9b adds exactly 6 mandatory OpenBao tests. Parse-error path is deferred (§4.5) — not 7.

---

## 10. Definition of Done

- [ ] `tests/conftest.py` has 4 new fixtures + 1 helper (openbao_container session-scoped; openbao_endpoint, openbao_token, openbao_bad_token function-scoped; seed_secret helper)
- [ ] Module-top `pytest.importorskip("requests")` ONLY; testcontainers gating happens inside fixture body per §4 (two-level pattern from P9a)
- [ ] 6 mandatory test categories all green on real OpenBao container (happy path, HC-B 404, HC-C-auth, HC-C-network, HC-D auth+network leakage, workspace_id boundary). HC-D scans the SENTINEL that was transmitted in each failing request — bad-token sentinel for auth path, good-token sentinel for network path.
- [ ] No engine + adapter diff: `git diff v1.1.1..HEAD -- claw_engine/` → 0 bytes
- [ ] All 4 P8d Hard Contracts still pass (run `pytest tests/adapters/test_openbao_secret.py`)
- [ ] `[integration]` extra updated to `testcontainers[postgres,vault]>=4.0`
- [ ] `tests/integration/README.md` updated with new case counts + OpenBao invocation
- [ ] Install matrix verified across 3 states (clean / partial / full)
- [ ] Proof-of-execution: `assert False` injection causes FAIL (proves real test runs)
- [ ] **Both sentinels** absent from corresponding error str/repr/args:
      - `OPENBAO-TEST-ROOT-DO-NOT-LEAK` (good token) NOT present in network-error path's `SecretProviderError`
      - `OPENBAO-BAD-TOKEN-DO-NOT-LEAK` (bad token) NOT present in auth-error path's `SecretProviderError`
      - Each test scans the sentinel actually transmitted in its failing request — NOT the unused one
- [ ] Default CI unchanged: `pytest -q -m "not integration"` → 519 + N deselected (N = existing 12 + new OpenBao integration count)
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 11. Locked Decisions (no confirm round needed)

1. **Scope: OpenBao integration only**; MySQL → P9a.1; adapter changes forbidden
2. **`testcontainers[vault]` extras + OpenBao image override** for the VaultContainer wrapper
3. **`openbao/openbao:2.0.0` pinned minor** (implementer verifies tag is published; bumps explicitly if not)
4. **Two sentinel tokens** —
   `_TEST_TOKEN_SENTINEL = "OPENBAO-TEST-ROOT-DO-NOT-LEAK"` (the good dev root token, set via
   `BAO_DEV_ROOT_TOKEN_ID`) AND `_TEST_BAD_TOKEN_SENTINEL = "OPENBAO-BAD-TOKEN-DO-NOT-LEAK"`
   (used by HC-D auth-error tests). The two-token design forces explicit "which sentinel was
   actually transmitted in this failing request?" thinking and avoids the trap of scanning the
   wrong sentinel.
5. **OpenBao env vars** — `BAO_DEV_ROOT_TOKEN_ID` + `BAO_DEV_LISTEN_ADDRESS` set via
   `.with_env(...)`; do NOT rely on testcontainers' `VAULT_*` alias being honored by OpenBao
6. **`requests` library for fixture seeding** — no `hvac` (mirrors P8d adapter decision)
7. **No `[hvac]` extras added** — keeps deps minimal
8. **No TLS / mTLS / auth-method-beyond-token testing** in P9b — separate plan
9. **No cross-restart durability test** — secrets persistence is OpenBao's concern; dev mode loses data on restart
10. **Pure test-only PR**: zero engine/adapter diff vs `v1.1.1`
11. **Install matrix verified in 3 states** with proof-of-execution check (P9a P1-#3 lesson)
12. **Module-top `importorskip("requests")` only; fixture-body `importorskip("testcontainers")`** —
    two-level pattern P9a finalized. requests is gated at module top because the test file uses
    it directly; testcontainers is gated in fixture body so partial installs skip cleanly.
13. **6 mandatory test categories** (§4 above) pinned at 6 — implementer may NOT add a 7th
    without sub-plan amendment. Parse-error path explicitly deferred (§4.5).
14. **VaultContainer primary, generic DockerContainer fallback** if VaultContainer's healthcheck
    doesn't accept OpenBao's `/v1/sys/health` response shape — implementer tries primary first,
    documents fallback choice in fixture docstring if used

---

## 12. Acceptance Focus (per user style, single sentence)

> Real OpenBao container exercises HC-A/B/C/D end-to-end; HC-D verified by two-sentinel leak
> scan across **auth + network real-error paths** (parse path deferred to P8d fake-HTTP unit
> tests per §4.5); install matrix 3-state verified (no silent false-green like P9a P1-#3);
> engine + adapter zero diff vs v1.1.1; default CI unchanged.

The single most important test is **§4.5 real-error token leakage** — fake HTTP can't fully
model real OpenBao error responses, and the auth-failure path is the most likely to leak token
metadata via requests library's exception representations. The two-sentinel design forces explicit
"which token was transmitted in this failing request?" thinking, preventing the obvious-looking
trap where the scan checks the unused sentinel and false-greens.
