"""P9b integration tests: OpenBaoSecretProvider against real OpenBao via testcontainers.

These tests use testcontainers to spin up a real OpenBao container in dev mode.
They exercise the 4 Hard Contracts (HC-A/B/C/D) end-to-end against a real backend
— fake-HTTP tests (P8d) verify the adapter logic in isolation; these verify that
the logic holds against a real OpenBao deployment.

Why real-backend tests matter (P9b rationale):
  Fake HTTP tests pass while a real OpenBao deployment could fail for reasons fake
  tests don't model: readiness-probe race conditions, real TLS handshake failures,
  KV v2 API quirks specific to OpenBao (vs HashiCorp Vault), and requests library
  exception representations that may include token metadata on real error paths.
  HC-D (token leakage) is the highest-priority real-backend concern — P9b closes
  that gap.

Parse-error path deferred (§4.5 of P9b sub-plan):
  test_real_openbao_parse_error is NOT in this file. Triggering a malformed-200
  response from real OpenBao requires proxying through a fake server, which
  contradicts the "real backend" intent. P8d's fake-HTTP unit tests cover the
  parse path comprehensively. If a future user-reported bug surfaces a real-OpenBao
  parse path that leaks a token, that triggers a separate PR — not P9b scope.

Two-sentinel HC-D design (§4.5):
  HC-D has two tests, not one. Each scans the sentinel ACTUALLY TRANSMITTED in
  its failing request:
  - test_real_openbao_auth_error_does_not_leak_bad_token: uses openbao_bad_token
    (bad sentinel transmitted → scan for it in error paths)
  - test_real_network_error_does_not_leak_good_token: uses openbao_token
    (good sentinel transmitted → scan for it in error paths)
  Mixing these up (e.g. auth test scans good token) silently false-greens because
  the good token was never transmitted in the failing auth request.

Run with:
    pytest -q -m integration tests/
    (use `tests/` NOT `tests/integration/` — the postgres contract cases would
    be missed by a `tests/integration/`-only invocation)

Prerequisites:
    pip install -e ".[secrets-openbao,integration]"
    Docker daemon running
"""
from __future__ import annotations

import pytest

# Two-level importorskip pattern (P9a final lesson, P9b §4):
#   Module top: importorskip("requests") ONLY — this file uses requests directly
#   via the seed_secret fixture. The importorskip here produces an informative
#   Skipped if someone installs [integration] without [secrets-openbao].
#   Fixture body (tests/conftest.py): importorskip("testcontainers") — gates the
#   container infra so partial installs skip cleanly rather than error at collection.
#   Do NOT importorskip("testcontainers") here — that would break the verified
#   two-level pattern from P9a.
pytest.importorskip(
    "requests",
    reason=(
        "requests not installed — OpenBao integration tests require the secrets-openbao "
        "extras. Install with: pip install -e '.[secrets-openbao,integration]'"
    ),
)

# Safe to import now (requests gate passed).
from claw_engine.adapters.secrets.openbao.errors import SecretProviderError  # noqa: E402
from claw_engine.adapters.secrets.openbao.provider import OpenBaoSecretProvider  # noqa: E402


# ---------------------------------------------------------------------------
# HC-D helper — reused across auth + network leakage tests
# ---------------------------------------------------------------------------


def _assert_no_token_leak(exc: SecretProviderError, sentinel: str) -> None:
    """Assert the sentinel does NOT appear in any default-output path of exc.

    Caller passes the sentinel that was ACTUALLY TRANSMITTED in the failing
    request — not the unrelated sentinel for a different code path.

    Checks:
      - str(exc)   → __str__ (stage + status + message)
      - repr(exc)  → __repr__ (class name + stage + status)
      - exc.args   → base Exception args tuple (args[0] = message)
    """
    assert sentinel not in str(exc), (
        f"Token sentinel leaked in str(exc): {str(exc)!r}"
    )
    assert sentinel not in repr(exc), (
        f"Token sentinel leaked in repr(exc): {repr(exc)!r}"
    )
    for arg in exc.args:
        assert sentinel not in str(arg), (
            f"Token sentinel leaked in exc.args element: {arg!r}"
        )


# ---------------------------------------------------------------------------
# §4.1 Happy path: seed and read
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_secrets_returns_seeded_kv_v2_data(
    openbao_endpoint: str,
    openbao_token: str,
    seed_secret,
) -> None:
    """HC-A/B happy path: seed KV v2 secrets → get_secrets returns exact mapping.

    Verifies:
    - Constructor validates endpoint + token without error
    - get_secrets hits real OpenBao KV v2 API at /v1/secret/data/...
    - Returns all seeded key-value pairs with string values
    - No extra/missing keys
    """
    seed_secret("claw/workspaces/ws-happy", {"OPENAI_KEY": "sk-test", "PORT": "8080"})
    provider = OpenBaoSecretProvider(
        endpoint=openbao_endpoint,
        token=openbao_token,
    )
    result = provider.get_secrets("ws-happy")
    assert result == {"OPENAI_KEY": "sk-test", "PORT": "8080"}


# ---------------------------------------------------------------------------
# §4.2 HC-B: 404 → {} (unseeded workspace)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_secrets_returns_empty_on_unseeded_workspace(
    openbao_endpoint: str,
    openbao_token: str,
) -> None:
    """HC-B: workspace with no seeded secrets returns {}, NOT raise.

    Real OpenBao returns HTTP 404 for a non-existent KV v2 path.
    Verifies the adapter converts 404 → empty dict (not SecretProviderError).
    """
    provider = OpenBaoSecretProvider(
        endpoint=openbao_endpoint,
        token=openbao_token,
    )
    result = provider.get_secrets("ws-not-seeded-never-exists")
    assert result == {}
    assert isinstance(result, dict)  # not None, not falsy-but-non-dict


# ---------------------------------------------------------------------------
# §4.3 HC-C-auth: bad token → SecretProviderError(stage="auth")
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_secrets_raises_on_bad_token(
    openbao_endpoint: str,
) -> None:
    """HC-C: 401/403 from real OpenBao → SecretProviderError(stage='auth').

    The bad token bypasses HC-A's constructor validation (token format is not
    validated beyond non-empty string — only endpoint format has shape checks).
    Real OpenBao rejects the token and returns 403 (or 401 depending on version).

    Note: this test does NOT use openbao_bad_token fixture — that sentinel is
    reserved for HC-D leakage scanning (§4.5). Here we just need any invalid
    token that OpenBao will reject.
    """
    bad_token = "INVALID-BUT-WELL-FORMED-TOKEN"
    provider = OpenBaoSecretProvider(
        endpoint=openbao_endpoint,
        token=bad_token,
    )

    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")

    assert exc_info.value.stage == "auth"
    assert exc_info.value.status in (401, 403)


# ---------------------------------------------------------------------------
# §4.4 HC-C-network: closed port → SecretProviderError(stage="network")
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_secrets_raises_on_unreachable_endpoint(
    openbao_token: str,
) -> None:
    """HC-C: network error → SecretProviderError(stage='network').

    Uses port 1 (unassigned; connection refused immediately) — avoids stopping
    the session-scoped container mid-test which would break parallel test isolation.

    Verifies the adapter distinguishes unreachable-endpoint from auth/parse failures.
    """
    provider = OpenBaoSecretProvider(
        endpoint="http://127.0.0.1:1",  # unassigned port; refused immediately
        token=openbao_token,
        timeout_connect=2.0,  # fast fail
    )

    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")

    assert exc_info.value.stage == "network"


# ---------------------------------------------------------------------------
# §4.5 HC-D: real-error token leakage (two-sentinel design)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_openbao_auth_error_does_not_leak_bad_token(
    openbao_endpoint: str,
    openbao_bad_token: str,
) -> None:
    """HC-D: real auth failure (401/403) must NOT echo the bad token in error str/repr/args.

    Uses openbao_bad_token (= 'OPENBAO-BAD-TOKEN-DO-NOT-LEAK') — the token that was
    ACTUALLY TRANSMITTED in the failing request. The good root token (openbao_token)
    is NOT relevant here because it was never transmitted on this code path.

    Two-sentinel rationale (§4.5 sub-plan):
      Scanning the wrong sentinel (e.g. scanning good token in auth path) silently
      false-greens — the good token was never transmitted so it can't appear in the
      error regardless of leakage. Each HC-D test MUST scan the sentinel it sent.
    """
    provider = OpenBaoSecretProvider(
        endpoint=openbao_endpoint,
        token=openbao_bad_token,
    )

    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")

    assert exc_info.value.stage == "auth"
    _assert_no_token_leak(exc_info.value, openbao_bad_token)


@pytest.mark.integration
def test_real_network_error_does_not_leak_good_token(
    openbao_token: str,
) -> None:
    """HC-D: real network error (connection refused) must NOT echo the good token.

    Uses openbao_token (= 'OPENBAO-TEST-ROOT-DO-NOT-LEAK') — the token that was
    ACTUALLY TRANSMITTED in the failing request (the good token is sent, then the
    connection is refused before any response comes back). The bad token is not
    relevant here because it was never transmitted on this code path.

    Two-sentinel rationale (§4.5 sub-plan):
      Scanning the bad sentinel in a network-error test would also false-green
      because the bad token was never sent. This test scans the good sentinel.
    """
    provider = OpenBaoSecretProvider(
        endpoint="http://127.0.0.1:1",  # closed port; refused immediately
        token=openbao_token,
        timeout_connect=2.0,
    )

    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-1")

    assert exc_info.value.stage == "network"
    _assert_no_token_leak(exc_info.value, openbao_token)


# ---------------------------------------------------------------------------
# §4.6 workspace_id boundary on real backend (HC-A / C1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_secrets_rejects_invalid_workspace_id_before_http(
    openbao_endpoint: str,
    openbao_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HC-A / C1: workspace_id validation fires BEFORE any HTTP request.

    Validates that the structural defense (path-traversal guard at adapter boundary)
    applies on a real backend too — the adapter must validate workspace_id before
    constructing the URL, not after. If an HTTP request is attempted, the monkeypatched
    requests.get raises AssertionError (test fails immediately).

    This also verifies the fix is end-to-end: even with a live OpenBao endpoint,
    an invalid workspace_id never triggers an HTTP call.
    """
    provider = OpenBaoSecretProvider(
        endpoint=openbao_endpoint,
        token=openbao_token,
    )

    def _bomb(*args, **kwargs):
        raise AssertionError(
            "HTTP request attempted despite invalid workspace_id — "
            "validation must happen BEFORE HTTP"
        )

    monkeypatch.setattr("requests.get", _bomb)

    with pytest.raises(ValueError):
        provider.get_secrets("../escape")
