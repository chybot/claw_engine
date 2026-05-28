# P8d: OpenBaoSecretProvider Adapter — Sub-Plan

**Parent plan:** `/Users/lucas.xu/.claude/plans/cryptic-twirling-sonnet.md` § Tier-2 / P8d
**Baseline:** main @ `83445b5` (after PR #3 P8c merge), tag `v1.0.0`
**Branch:** `feat/p8d-openbao-secret`

---

## Why this sub-plan exists

P8d's risk is **HTTP error taxonomy drift**. The engine's `SecretProvider` Protocol returns
`Mapping[str, str]`; the V1 `InMemorySecretProvider` returns `{}` for unregistered workspaces;
any deviation (e.g. raising on missing workspace, or silently returning `{}` on auth failure)
breaks engine assumptions and creates security smells. User specifically requested **three
hard contracts on HTTP error semantics**. Plus token handling (a secret-managing adapter must
not leak its own auth token).

This adapter is simpler than P8b/P8c — no subprocess, no policy file, just HTTP. Sub-plan is
shorter. The 4 hard contracts are the substance.

---

## 1. Lifecycle (3 phases, strict separation)

| Phase | Methods | Allowed | Forbidden |
|---|---|---|---|
| **Construction** | `__init__(endpoint, token, *, mount="secret", path_template=..., timeout=...)` | argument validation (pure-string checks) | network (no HTTP), subprocess, disk write |
| **Read** | `get_secrets(workspace_id) -> Mapping[str, str]` | HTTP GET against KV v2 endpoint | writing to OpenBao (out of scope V1) |
| **Adapter-local control** | (none in V1) | — | save_token, mutate_state — none exist |

Construction is hermetic. **NO HTTP probe of OpenBao at init time** (no "warmup", no
auth-check call). First HTTP request happens on the first `get_secrets()` call.

---

## 2. Constructor Signature

```python
class OpenBaoSecretProvider(SecretProvider):
    def __init__(
        self,
        endpoint: str,                                  # e.g. "https://openbao.internal:8200"
        token: str,                                     # OpenBao token; redacted in repr
        *,
        mount: str = "secret",                          # KV v2 mount point
        path_template: str = "claw/workspaces/{workspace_id}",  # path under mount
        timeout_connect: float = 5.0,
        timeout_read: float = 30.0,
    ) -> None: ...
```

Validation in `__init__` (all pure-string):
- `endpoint` is a non-empty string starting with `http://` or `https://`. Strip trailing slash for normalisation. Reject `file://` and other schemes (would break URL composition).
- `token` is a non-empty string. **Stored on `self._token`, never logged, never in `__repr__`** (HC-D).
- `mount` is a non-empty string, no leading/trailing slash, no `/` inside (single segment).
- `path_template` contains exactly one `{workspace_id}` placeholder; contains no `..` segments after substitution (will be checked at `get_secrets` time too, defensively).
- `timeout_connect` / `timeout_read` are positive floats.

Any violation → `ValueError` (caller input error, like Casbin's I2 fix).

---

## 3. KV v2 Path Semantics (locked)

OpenBao/Vault KV v2 has two URL forms per logical path:

| Endpoint | Purpose | Used by P8d? |
|---|---|---|
| `GET /v1/<mount>/data/<path>` | Read latest version of secrets at `<path>` | **YES** — this is what `get_secrets` calls |
| `GET /v1/<mount>/metadata/<path>` | List versions, ACLs, etc. | NO — out of scope V1 |
| `POST /v1/<mount>/data/<path>` | Write a new version | NO — V1 is read-only |

Composed URL for `get_secrets(workspace_id="ads-team")` with defaults:

```
GET {endpoint}/v1/secret/data/claw/workspaces/ads-team
Headers:
  X-Vault-Token: {self._token}
  Accept: application/json
```

KV v2 response shape (200):
```json
{
  "data": {
    "data": {                  // ← actual secrets are HERE
      "OPENAI_API_KEY": "sk-...",
      "SEATALK_TOKEN": "..."
    },
    "metadata": {              // ← version info, NOT secrets — drop on the floor
      "created_time": "...",
      "version": 3,
      ...
    }
  }
}
```

`get_secrets()` returns `response["data"]["data"]` as `Mapping[str, str]`. If any value is not
a string (e.g. a JSON number), coerce via `str(value)` and log nothing. If `response["data"]["data"]`
is missing or malformed → `SecretProviderError` (HC-C2 in §5 below).

---

## 4. HTTP Client Choice

Use **`requests`** (PyPI, ~50M downloads/mo, stable API since 2.x). Reasons:
- Adapter only needs synchronous `GET` with header + timeout
- `hvac` (the Vault official client) adds ~5MB of dependencies and bespoke abstractions for
  a 30-line problem
- Fake HTTP server in tests (HC-B/C verification) is trivially intercepted with `requests-mock`,
  `responses`, or `pytest-httpserver`

`requests` is already a transitive dep via `casbin` and others. Adding it to `[secrets-openbao]`
extras explicit is honest.

**No `hvac`.** Adapter constructs its own URLs from `mount` + `path_template` + `workspace_id`.

---

## 5. Hard Contracts (locked, non-negotiable)

### Hard Contract HC-A — Construction is hermetic (no HTTP at init)

`__init__` performs pure-string validation only. It MUST NOT make any HTTP request,
not even a `/v1/sys/health` ping or token validation.

**Required tests:**
- `test_construction_does_no_http`: monkey-patch `requests.get` / `requests.post` to raise on
  call; construction succeeds for any valid kwargs.
- `test_construction_validates_endpoint_scheme`: `file:///etc/secret` → `ValueError`.

### Hard Contract HC-B — Workspace not found ⇒ `{}` (NOT error)

OpenBao returns HTTP 404 when a secret path doesn't exist. `get_secrets` MUST return `{}`
(empty mapping) for this case. This mirrors V1 `InMemorySecretProvider` behaviour for
unregistered workspaces: a workspace having no secrets configured is **not an error**.

**Required test:**
- `test_get_secrets_returns_empty_on_404`: fake HTTP server returns 404 for any path;
  `get_secrets("unregistered-ws")` returns `{}` (empty dict, not None, not raise).
- The `mtime` of `policy.csv` is irrelevant here — there's no policy.csv. The contract is
  purely about the response code mapping.

### Hard Contract HC-C — Auth failure or timeout ⇒ `SecretProviderError` (adapter-local)

The adapter has its own `SecretProviderError` (lives in `errors.py`, NOT in engine contract).
The engine `SecretProvider` Protocol has NO defined exception types — `get_secrets` can raise
anything in principle, but the adapter promise is: anything other than HTTP 200 (with valid
KV v2 body) or HTTP 404 maps to `SecretProviderError`.

Concretely:

| Backend response | Adapter behavior |
|---|---|
| HTTP 200 with KV v2 body | return `data["data"]["data"]` |
| HTTP 404 | return `{}` (HC-B) |
| HTTP 401 (bad token) | raise `SecretProviderError(stage="auth", status=401, ...)` |
| HTTP 403 (forbidden) | raise `SecretProviderError(stage="auth", status=403, ...)` |
| HTTP 5xx | raise `SecretProviderError(stage="backend", status=5xx, ...)` |
| Timeout (connect or read) | raise `SecretProviderError(stage="timeout", ...)` |
| Connection error (DNS, refused, network) | raise `SecretProviderError(stage="network", ...)` |
| JSON parse error or malformed KV v2 body | raise `SecretProviderError(stage="parse", ...)` |

**Required tests:**
- `test_get_secrets_raises_on_401`: fake server → 401; assert
  `pytest.raises(SecretProviderError, match="auth")`
- `test_get_secrets_raises_on_403`: fake server → 403; same shape
- `test_get_secrets_raises_on_500`: fake server → 500; `stage="backend"`
- `test_get_secrets_raises_on_timeout`: fake server delays beyond `timeout_read`; assert
  `pytest.raises(SecretProviderError, match="timeout")`
- `test_get_secrets_raises_on_connection_refused`: point at unreachable port; assert
  `pytest.raises(SecretProviderError, match="network")`
- `test_get_secrets_raises_on_malformed_body`: fake server → 200 with `{"unexpected": "shape"}`;
  assert `SecretProviderError(stage="parse")`

### Hard Contract HC-D — Token never leaks via any default-output path

A secret-managing adapter that itself leaks its auth token is a worse problem than not using
the adapter. `requests` exceptions are especially leaky — `HTTPError.__str__` often includes
the full request URL; connection errors can include SSL handshake details; debug reprs of
`PreparedRequest` include headers. P8d must defensively scrub all of these.

**5 specific sub-contracts (all enforced by tests):**

**HC-D.1 — Adapter's own `repr` and `str` exclude token**
`OpenBaoSecretProvider.__repr__` and `__str__` must NOT contain `self._token`. Acceptable
fields to expose: `endpoint` (base URL only, no token), `mount`, `path_template`, timeout
values, a redacted token marker (e.g. `token=<redacted>`). Default Python `__repr__` inherits
from `object` which does NOT print attributes — so we either implement a custom `__repr__`
explicitly excluding token, or trust the default and add a test that confirms `_token`
attribute access is the only path to the value.

**HC-D.2 — `SecretProviderError.__str__` and `repr(error)` exclude token**
The adapter's exception class formats messages from `stage`, `status` (HTTP code), and a
short description. NEVER include the token value, NEVER include full `requests.Request` /
`requests.Response` objects (their default repr leaks headers).

**HC-D.3 — When wrapping `requests` exceptions, never include request headers in the message**
The `requests` library's exception chain often retains a `.request` attribute on `HTTPError`
that has full headers including `X-Vault-Token`. When catching a `requests` exception and
building a `SecretProviderError`, the new exception's message MUST be constructed from
non-leaky fields only — never `str(original_exception)` if that exception is the kind that
serializes its request (e.g. `urllib3.exceptions.MaxRetryError`).

Implementation pattern:
```python
try:
    response = requests.get(url, headers=headers, timeout=(t_connect, t_read))
except requests.exceptions.Timeout:
    raise SecretProviderError(stage="timeout", status=None,
                              message=f"OpenBao request timed out for {self._safe_url_for_error(workspace_id)}") from None
except requests.exceptions.ConnectionError:
    raise SecretProviderError(stage="network", status=None,
                              message=f"OpenBao connection failed for {self._safe_url_for_error(workspace_id)}") from None
```

Note `from None` to suppress the implicit `__context__` chain in tracebacks — otherwise
Python shows the original `requests` exception traceback alongside, which may leak headers
via the framing.

**HC-D.4 — `_safe_url_for_error` returns base_url + path only**
Helper method that returns the URL form usable in error messages:
- Allowed: `{endpoint}/v1/{mount}/data/{path}` (base URL + path)
- Forbidden: query string (`?...`), fragment (`#...`), embedded credentials
  (`https://user:pass@host`), Authorization-header echo
The helper MUST strip query and fragment defensively even if the URL ever ends up with them
(belt + suspenders; adapter never constructs them, but defensive code prevents future
regressions).

**HC-D.5 — Test scanner uses recognizable sentinel token**
Tests instantiate adapter with `token="PLAINTEXT-TOKEN-DO-NOT-LEAK"`. Every test that
triggers any error path or default-output path then scans:
- `repr(provider)` — must not contain `"PLAINTEXT-TOKEN-DO-NOT-LEAK"`
- `str(provider)` — same
- `str(exception)` for each `SecretProviderError` branch — same
- `repr(exception)` for each — same
- `exception.args` (each element coerced via `str()`) — same
- Final assertion: `"PLAINTEXT-TOKEN-DO-NOT-LEAK" not in <every default-output text>`

Implement as a shared assertion helper `assert_no_token_leak(text: str)` reused across all
HC-D tests.

**Required tests** (all use the sentinel token):
- `test_repr_does_not_leak_token` (HC-D.1)
- `test_str_does_not_leak_token` (HC-D.1)
- `test_secret_provider_error_str_does_not_leak_token` (HC-D.2)
- `test_secret_provider_error_repr_does_not_leak_token` (HC-D.2)
- `test_error_does_not_leak_token_on_401` (HC-D.2, HC-D.3 — triggers auth error, scans message)
- `test_error_does_not_leak_token_on_403` (same shape)
- `test_error_does_not_leak_token_on_500` (same shape)
- `test_error_does_not_leak_token_on_timeout` (HC-D.3 — confirms `from None` strips chain)
- `test_error_does_not_leak_token_on_connection_refused` (HC-D.3)
- `test_error_does_not_leak_token_on_malformed_body` (HC-D.2)
- `test_safe_url_for_error_strips_query_and_fragment` (HC-D.4) — pass a hypothetical URL
  with query+fragment, assert helper returns base+path only
- `test_safe_url_for_error_excludes_credentials` (HC-D.4) — pass URL with embedded creds,
  assert helper strips them

Token may appear in `self._token` (internal attribute access is fine) but not in any
default-output path. Debugger users accessing `_token` explicitly is acceptable.

---

## 6. File Layout

```
claw_engine/adapters/secrets/
├── __init__.py                  # empty (package stub)
└── openbao/
    ├── __init__.py              # exports OpenBaoSecretProvider, SecretProviderError
    ├── provider.py              # main class
    └── errors.py                # SecretProviderError (adapter-local, with stage + status fields)

tests/adapters/
└── test_openbao_secret.py      # invariant tests + 4 Hard Contract tests

tests/integration/               # NEW directory (P8d introduces it)
└── test_openbao_secret_integration.py  # testcontainers OpenBao, @pytest.mark.integration

pyproject.toml addition:
  [project.optional-dependencies]
  secrets-openbao = ["requests>=2.30"]
  
  [tool.pytest.ini_options]
  markers = ["integration: requires testcontainers (mysql, openbao, etc.)"]
```

`provider.py` soft cap: 200 lines. Class is small (1 init + 1 public method + 2-3 helpers).
If it grows beyond, flag.

---

## 7. Engine Integration

The adapter implements `engine/context/secrets.py::SecretProvider` Protocol. Composition:

```python
from claw_engine.adapters.secrets.openbao import OpenBaoSecretProvider

secrets = OpenBaoSecretProvider(
    endpoint="https://openbao.internal:8200",
    token=os.environ["OPENBAO_TOKEN"],
    mount="secret",
    path_template="claw/workspaces/{workspace_id}",
)

# Pass to WorkspaceResolver alongside ConfigProvider:
resolver = WorkspaceResolver(
    cwd_root="/path/to/workspaces",
    config_provider=...,
    secret_provider=secrets,
)
```

The engine doesn't know OpenBao exists. The adapter satisfies the Protocol.

**Engine-side redact path stays UNCHANGED**: `WorkspaceResolver` already merges secrets into
`ResolvedWorkspace.env` and `ResolvedWorkspace.__repr__` already redacts. P8d does NOT touch
any redaction logic in engine.

---

## 8. Test Invariants (`tests/adapters/test_openbao_secret.py`)

⭐ = acceptance-critical (must pass before merge).

**Construction**

1. ⭐ **HC-A: Construction is hermetic** — `test_construction_does_no_http` (monkey-patch
   `requests.get` to bomb; constructor succeeds).
2. **Validation** — `test_construction_rejects_bad_endpoint_scheme` (`file://`, `""`),
   `test_construction_rejects_empty_token`, `test_construction_rejects_bad_path_template`
   (missing `{workspace_id}`, contains `..`), `test_construction_rejects_negative_timeouts`.

**Read happy path**

3. **Returns secrets on 200** — fake server returns valid KV v2 body; `get_secrets("ws")`
   returns the inner `data.data` mapping.
4. **Drops metadata** — fake server's response includes `data.metadata`; the result mapping
   excludes those keys (only `data.data` is returned).
5. **Coerces non-string values** — fake server returns `{"PORT": 8080}` (int); result has
   `{"PORT": "8080"}` (string).

**HC-B**

6. ⭐ **404 → `{}`** — `test_get_secrets_returns_empty_on_404`.

**HC-C error matrix**

7. ⭐ **401 → SecretProviderError** — `test_get_secrets_raises_on_401`, `match="auth"`.
8. ⭐ **403 → SecretProviderError** — `test_get_secrets_raises_on_403`, `match="auth"`.
9. ⭐ **5xx → SecretProviderError** — `test_get_secrets_raises_on_500`, `stage="backend"`.
10. ⭐ **Timeout → SecretProviderError** — `test_get_secrets_raises_on_timeout`,
    `match="timeout"`.
11. ⭐ **Connection error → SecretProviderError** — `test_get_secrets_raises_on_connection_refused`,
    `match="network"`.
12. ⭐ **Malformed body → SecretProviderError** — `test_get_secrets_raises_on_malformed_body`,
    `stage="parse"`.

**HC-D**

13. ⭐ **repr/str don't leak token** — `test_repr_does_not_leak_token`,
    `test_str_does_not_leak_token`.
14. ⭐ **error messages don't leak token** — trigger every HC-C branch with a recognisable
    token literal like `"super-secret-token-DO-NOT-LOG"`; assert it never appears in
    `str(exc)` or `exc.args`.

**HTTP request shape**

15. **Headers include X-Vault-Token** — fake server records inbound request; assert
    `headers["X-Vault-Token"] == token`.
16. **URL composed correctly** — `GET {endpoint}/v1/{mount}/data/{path_template_filled}`.
17. **Timeout passed to requests** — verify `requests.get(...)` got `timeout=(connect, read)`
    tuple. (Tested via monkey-patching `requests.get` to capture kwargs.)

**Protocol compatibility**

18. **isinstance check** — `isinstance(provider, SecretProvider) is True` (runtime_checkable).

**Cross-impl contract**

19. **Shared SecretProvider contract suite** — parametrise an in-memory + OpenBao both running
    a tiny contract that covers `get_secrets` happy and missing-workspace paths. Optional if it
    adds disproportionate complexity; current `InMemorySecretProvider` is in
    `claw_engine/engine/context/secrets.py`.

---

## 9. Integration Tests (`tests/integration/test_openbao_secret_integration.py`)

Marked `@pytest.mark.integration`. Default CI does NOT run these (use
`pytest -m "not integration"` as default, `pytest -m integration` to opt in).

Setup: `testcontainers` Python lib launches an OpenBao container, seeds a secret at
`secret/data/claw/workspaces/test-ws`, exercises the adapter against the real container.

Test cases (3, focused — these prove the fake matches reality):

1. **End-to-end happy path** — seed a secret, `get_secrets("test-ws")` returns expected map.
2. **End-to-end 404** — `get_secrets("does-not-exist")` returns `{}`.
3. **End-to-end auth failure** — adapter with bad token raises `SecretProviderError`.

If a full container test is too heavy for one PR, defer 1-3 to a follow-up `tests/integration/`
infrastructure PR. **The default-CI fake HTTP tests are mandatory; integration tests are
nice-to-have for V1.**

---

## 10. pyproject extras

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []
identity-casbin = ["casbin>=1.30"]
secrets-openbao = ["requests>=2.30"]

[tool.pytest.ini_options]
# ... existing settings ...
markers = [
    "integration: requires running infrastructure (testcontainers) — skipped in default CI",
]
```

Test gating: `pytest.importorskip("requests")` at top of `test_openbao_secret.py`.
For integration: `pytest.importorskip("testcontainers")` at top of integration test file.

---

## 11. Definition of Done

- [ ] All 18+ invariant tests passing (19 if contract suite added)
- [ ] All 4 Hard Contracts (HC-A through HC-D) have at least one ⭐ test
- [ ] `pytest -q` → 296 + new tests, all green
- [ ] `pytest tests/purity -q` → 2/2 (engine still clean)
- [ ] `git diff v1.0.0..HEAD -- claw_engine/engine/` → empty
- [ ] `grep -rn "from claw_engine.adapters" claw_engine/engine/` → empty
- [ ] `grep -rn "requests" claw_engine/engine/` → empty
- [ ] `grep -rn "openbao\|hvac\|vault" claw_engine/engine/` → empty
- [ ] **HC-A**: monkey-patch test proves construction does no HTTP
- [ ] **HC-B**: 404 → `{}` (NOT `None`, NOT raise)
- [ ] **HC-C**: all 6 error categories (401, 403, 5xx, timeout, network, parse) → `SecretProviderError`
- [ ] **HC-D**: token redacted from repr, str, all exception messages
- [ ] `[secrets-openbao]` extra (requests-only); default install has no openbao deps
- [ ] `integration` pytest marker registered in pyproject
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 12. Out of Scope (defer to later)

- **Write to OpenBao** (`set_secrets`) — V1 is read-only
- **KV v1** — only v2 supported (almost all modern installs)
- **Token rotation / TTL handling** — caller refreshes their `OpenBaoSecretProvider` when token
  rotates; adapter doesn't auto-renew
- **AppRole / OIDC auth** — V1 only takes pre-issued tokens; auth method is caller's problem
- **Versioned secret reads** — `get_secrets` always reads latest version
- **Metadata access** — workflow can't inspect created_time, version count, etc.
- **`hvac` Python client** — explicitly rejected (see §4); `requests` is enough
- **Concurrent request safety** — V1 is sync; `requests.Session` not pooled (one-shot
  `requests.get` per `get_secrets` call). If perf matters later, add `Session` reuse
- **Caching** — every `get_secrets` is a fresh HTTP call. Add caching only when needed
- **Integration tests** — fake HTTP is mandatory; testcontainers integration tests are
  nice-to-have for V1 (see §9)

---

## 13. Locked Decisions (no confirm round needed)

1. **`requests` library, not `hvac`** (§4)
2. **KV v2 read-only** via `GET /v1/<mount>/data/<path>` (§3)
3. **`mount` and `path_template` are constructor params**, not hardcoded; defaults sane for
   `secret/` + `claw/workspaces/{workspace_id}` (§2)
4. **Construction is hermetic** — no HTTP at init time (HC-A)
5. **404 → `{}`** — workspaces without secrets are not errors (HC-B)
6. **All other non-200 → `SecretProviderError`** — adapter-local error, not engine contract
   (HC-C)
7. **Token never in default-output paths** — repr, str, exception messages (HC-D)
8. **`SecretProviderError` has `stage` field** — `{"auth", "backend", "timeout", "network", "parse"}` —
   mirrors `GitSkillSourceError(stage=...)` taxonomy from P8b
9. **Connect timeout 5s, read timeout 30s** defaults; both overridable
10. **Integration tests live under `tests/integration/`** with `@pytest.mark.integration`
    marker; default CI does NOT run them
11. **No write methods** — V1 read-only
12. **No `Session` pooling** — V1 simple; one-shot `requests.get` per call

---

## 14. Acceptance Focus (single sentence)

> Construction is HTTP-free; 404 maps to empty mapping; every other non-200 (401, 403, 5xx,
> timeout, network, parse) maps to adapter-local `SecretProviderError`; the OpenBao token is
> never present in any default-output text (repr, str, exception messages); engine Protocol
> unchanged; redact path stays engine-side.

Any deviation from these five requires a new plan.
