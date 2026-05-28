"""P8d: OpenBaoSecretProvider — invariant + Hard Contract tests.

Fake HTTP intercepted via `responses` library (https://github.com/getsentry/responses).
Chosen over requests-mock/pytest-httpserver: battle-tested, minimal setup, supports
both decorator and context-manager usage.

Integration tests (testcontainers) are deferred to a follow-up PR — see module docstring
at the bottom of this file.
"""
from __future__ import annotations

import pytest
import requests

# Gate: skip entire module if requests is not installed.
pytest.importorskip("requests", reason="requests not installed (pip install -e .[secrets-openbao])")

# Gate: skip entire module if responses is not installed (test-only dep).
pytest.importorskip("responses", reason="responses not installed (pip install responses)")

import responses as responses_lib  # noqa: E402

from claw_engine.adapters.secrets.openbao import (  # noqa: E402
    OpenBaoSecretProvider,
    SecretProviderError,
)
from claw_engine.engine.context.secrets import SecretProvider  # noqa: E402

# ── Sentinel token used across all HC-D tests ────────────────────────────────

SENTINEL_TOKEN = "PLAINTEXT-TOKEN-DO-NOT-LEAK"

# ── Shared assertion helper (HC-D.5) ─────────────────────────────────────────


def assert_no_token_leak(text: str) -> None:
    """Assert that the sentinel token does not appear in `text`."""
    assert SENTINEL_TOKEN not in text, (
        f"Token leaked in output text. Offending string (truncated):\n"
        f"  {text[:200]!r}"
    )


# ── Default valid kwargs for constructing a provider ─────────────────────────

VALID_KWARGS = dict(
    endpoint="https://openbao.internal:8200",
    token=SENTINEL_TOKEN,
    mount="secret",
    path_template="claw/workspaces/{workspace_id}",
    timeout_connect=5.0,
    timeout_read=30.0,
)

KV2_HAPPY_BODY = {
    "data": {
        "data": {
            "OPENAI_API_KEY": "sk-test",
            "SEATALK_TOKEN": "st-abc",
        },
        "metadata": {
            "created_time": "2024-01-01T00:00:00Z",
            "version": 3,
        },
    }
}

# ── HC-A: Construction is hermetic ───────────────────────────────────────────


def test_construction_does_no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-A: __init__ must not make ANY HTTP request.

    Monkey-patch requests.get and requests.post to raise; construction must succeed.
    """
    def bomb_get(*args, **kwargs):
        raise AssertionError("requests.get must not be called in __init__")

    def bomb_post(*args, **kwargs):
        raise AssertionError("requests.post must not be called in __init__")

    monkeypatch.setattr(requests, "get", bomb_get)
    monkeypatch.setattr(requests, "post", bomb_post)

    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    assert provider is not None


def test_construction_rejects_empty_endpoint() -> None:
    """HC-A / validation: empty endpoint → ValueError."""
    with pytest.raises(ValueError, match="endpoint"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "endpoint": ""})


def test_construction_rejects_bad_endpoint_scheme() -> None:
    """HC-A / validation: non-http(s) scheme → ValueError."""
    for bad in ("file:///etc/secret", "ftp://host", "ssh://host", "//host"):
        with pytest.raises(ValueError, match="endpoint"):
            OpenBaoSecretProvider(**{**VALID_KWARGS, "endpoint": bad})


def test_construction_rejects_empty_token() -> None:
    """HC-A / validation: empty token → ValueError."""
    with pytest.raises(ValueError, match="token"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "token": ""})


def test_construction_rejects_bad_mount() -> None:
    """HC-A / validation: mount with slash or empty → ValueError."""
    for bad in ("", "my/mount", "/leading", "trailing/"):
        with pytest.raises(ValueError, match="mount"):
            OpenBaoSecretProvider(**{**VALID_KWARGS, "mount": bad})


def test_construction_rejects_bad_path_template_missing_placeholder() -> None:
    """HC-A / validation: path_template missing {workspace_id} → ValueError."""
    with pytest.raises(ValueError, match="path_template"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "path_template": "claw/workspaces/fixed"})


def test_construction_rejects_path_template_with_dotdot() -> None:
    """HC-A / validation: path_template with '..' → ValueError."""
    with pytest.raises(ValueError, match="path_template"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "path_template": "claw/../{workspace_id}"})


def test_construction_rejects_negative_timeout_connect() -> None:
    """HC-A / validation: non-positive timeout_connect → ValueError."""
    with pytest.raises(ValueError, match="timeout"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "timeout_connect": 0.0})


def test_construction_rejects_negative_timeout_read() -> None:
    """HC-A / validation: non-positive timeout_read → ValueError."""
    with pytest.raises(ValueError, match="timeout"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "timeout_read": -1.0})


# ── I2: Endpoint must not carry credentials, query, or fragment ──────────────


@pytest.mark.parametrize(
    "bad_endpoint",
    [
        "https://user:pass@vault.internal:8200",
        "https://user@vault.internal:8200",
        "https://:pass@vault.internal:8200",
    ],
)
def test_construction_rejects_endpoint_with_credentials(bad_endpoint: str) -> None:
    """I2: endpoint with embedded user:pass → ValueError (prevents __repr__ leak)."""
    with pytest.raises(ValueError, match="credentials"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "endpoint": bad_endpoint})


@pytest.mark.parametrize(
    "bad_endpoint",
    [
        "https://vault.internal:8200?token=leak",
        "https://vault.internal:8200#fragment",
        "https://vault.internal:8200/?q=1",
        "https://vault.internal:8200/#f",
    ],
)
def test_construction_rejects_endpoint_with_query_or_fragment(bad_endpoint: str) -> None:
    """I2: endpoint with query string or fragment → ValueError."""
    with pytest.raises(ValueError, match="query or fragment"):
        OpenBaoSecretProvider(**{**VALID_KWARGS, "endpoint": bad_endpoint})


def test_construction_endpoint_credential_leak_message_excludes_secret() -> None:
    """I2: the rejection error itself must not echo the embedded password."""
    embedded_pw = "super-secret-pw-12345"
    bad = f"https://user:{embedded_pw}@vault.internal:8200"
    with pytest.raises(ValueError) as exc_info:
        OpenBaoSecretProvider(**{**VALID_KWARGS, "endpoint": bad})
    # Message intentionally omits the bad endpoint value so we don't echo creds
    assert embedded_pw not in str(exc_info.value)
    assert embedded_pw not in repr(exc_info.value)


# ── HC-B: 404 → {} ───────────────────────────────────────────────────────────


@responses_lib.activate
def test_get_secrets_returns_empty_on_404() -> None:
    """HC-B: HTTP 404 → empty mapping (not None, not raise)."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/unregistered-ws",
        status=404,
        json={"errors": []},
    )
    result = provider.get_secrets("unregistered-ws")
    assert result == {}
    assert result is not None


# ── HC-C: Error matrix ────────────────────────────────────────────────────────


@responses_lib.activate
def test_get_secrets_raises_on_401() -> None:
    """HC-C: HTTP 401 → SecretProviderError with stage='auth'."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=401,
        json={"errors": ["permission denied"]},
    )
    with pytest.raises(SecretProviderError, match="auth") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "auth"
    assert exc_info.value.status == 401


@responses_lib.activate
def test_get_secrets_raises_on_403() -> None:
    """HC-C: HTTP 403 → SecretProviderError with stage='auth'."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=403,
        json={"errors": ["forbidden"]},
    )
    with pytest.raises(SecretProviderError, match="auth") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "auth"
    assert exc_info.value.status == 403


@responses_lib.activate
def test_get_secrets_raises_on_500() -> None:
    """HC-C: HTTP 5xx → SecretProviderError with stage='backend'."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=500,
        json={"errors": ["internal server error"]},
    )
    with pytest.raises(SecretProviderError, match="backend") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "backend"
    assert exc_info.value.status == 500


def test_get_secrets_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-C: requests.Timeout → SecretProviderError with stage='timeout'."""
    def bomb(*args, **kwargs):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(requests, "get", bomb)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError, match="timeout") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "timeout"
    assert exc_info.value.status is None


def test_get_secrets_raises_on_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-C: ConnectionError → SecretProviderError with stage='network'."""
    def bomb(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", bomb)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError, match="network") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "network"
    assert exc_info.value.status is None


@responses_lib.activate
def test_get_secrets_raises_on_malformed_body() -> None:
    """HC-C: 200 with unexpected body shape → SecretProviderError with stage='parse'."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=200,
        json={"unexpected": "shape"},
    )
    with pytest.raises(SecretProviderError, match="parse") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "parse"


@responses_lib.activate
def test_get_secrets_raises_on_invalid_json() -> None:
    """HC-C: 200 with non-JSON body → SecretProviderError with stage='parse'."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=200,
        body=b"not json at all",
        content_type="text/plain",
    )
    with pytest.raises(SecretProviderError, match="parse") as exc_info:
        provider.get_secrets("ws")
    assert exc_info.value.stage == "parse"


# ── Happy path tests ──────────────────────────────────────────────────────────


@responses_lib.activate
def test_get_secrets_returns_inner_data_on_200() -> None:
    """Returns the inner data.data mapping from KV v2 response."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=200,
        json=KV2_HAPPY_BODY,
    )
    result = provider.get_secrets("ws")
    assert result == {"OPENAI_API_KEY": "sk-test", "SEATALK_TOKEN": "st-abc"}


@responses_lib.activate
def test_get_secrets_drops_metadata() -> None:
    """Metadata key from KV v2 is NOT in the returned mapping."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=200,
        json=KV2_HAPPY_BODY,
    )
    result = provider.get_secrets("ws")
    assert "metadata" not in result
    assert "created_time" not in result
    assert "version" not in result


@responses_lib.activate
def test_get_secrets_coerces_non_string_values() -> None:
    """Non-string values (int, float, bool) are coerced via str()."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws",
        status=200,
        json={
            "data": {
                "data": {"PORT": 8080, "ENABLED": True, "RATIO": 0.5},
                "metadata": {},
            }
        },
    )
    result = provider.get_secrets("ws")
    assert result == {"PORT": "8080", "ENABLED": "True", "RATIO": "0.5"}


# ── HTTP request shape tests ──────────────────────────────────────────────────


def test_headers_include_x_vault_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Vault-Token header is set to the token on each request."""
    captured: dict = {}

    def fake_get(url, *, headers, timeout, **kwargs):
        captured["headers"] = headers
        captured["url"] = url
        captured["timeout"] = timeout
        # Return a minimal valid KV v2 response
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"data": {"K": "V"}, "metadata": {}}}
            def raise_for_status(self):
                pass

        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(
        endpoint="https://openbao.internal:8200",
        token="my-secret-token",
        mount="secret",
        path_template="claw/workspaces/{workspace_id}",
    )
    provider.get_secrets("ws-test")
    assert captured["headers"]["X-Vault-Token"] == "my-secret-token"


def test_url_composed_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL follows GET {endpoint}/v1/{mount}/data/{path}."""
    captured: dict = {}

    def fake_get(url, *, headers, timeout, **kwargs):
        captured["url"] = url

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"data": {"K": "V"}, "metadata": {}}}
            def raise_for_status(self):
                pass

        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(
        endpoint="https://openbao.internal:8200",
        token="tok",
        mount="kv",
        path_template="workspaces/{workspace_id}/secrets",
    )
    provider.get_secrets("ads-team")
    assert captured["url"] == "https://openbao.internal:8200/v1/kv/data/workspaces/ads-team/secrets"


def test_timeout_tuple_passed_to_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout is passed as (connect, read) tuple."""
    captured: dict = {}

    def fake_get(url, *, headers, timeout, **kwargs):
        captured["timeout"] = timeout

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"data": {}, "metadata": {}}}
            def raise_for_status(self):
                pass

        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(
        endpoint="https://openbao.internal:8200",
        token="tok",
        timeout_connect=3.0,
        timeout_read=15.0,
    )
    provider.get_secrets("ws")
    assert captured["timeout"] == (3.0, 15.0)


def test_trailing_slash_stripped_from_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing slash on endpoint is stripped so URL composition is clean."""
    captured: dict = {}

    def fake_get(url, *, headers, timeout, **kwargs):
        captured["url"] = url

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"data": {}, "metadata": {}}}
            def raise_for_status(self):
                pass

        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(
        endpoint="https://openbao.internal:8200/",  # trailing slash
        token="tok",
        mount="secret",
        path_template="claw/{workspace_id}",
    )
    provider.get_secrets("ws")
    # Should NOT have double slash
    assert "//" not in captured["url"].replace("https://", "")


# ── Protocol compatibility ────────────────────────────────────────────────────


def test_isinstance_secret_provider() -> None:
    """isinstance(provider, SecretProvider) is True (runtime_checkable)."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    assert isinstance(provider, SecretProvider)


# ── HC-D: Token never leaks via any default-output path ─────────────────────


def test_repr_does_not_leak_token() -> None:
    """HC-D.1: repr(provider) must not contain the token."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    assert_no_token_leak(repr(provider))
    assert "<redacted>" in repr(provider) or "redacted" in repr(provider).lower()


def test_str_does_not_leak_token() -> None:
    """HC-D.1: str(provider) must not contain the token."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    assert_no_token_leak(str(provider))


def test_secret_provider_error_str_does_not_leak_token() -> None:
    """HC-D.2: str(SecretProviderError) must not contain the token.

    The adapter never passes the token into the error message. This test
    verifies that the error class itself doesn't inject any additional token
    leakage beyond what was explicitly passed. The adapter production path
    never passes the token — the sentinel-token tests in HC-D.3 below verify
    that directly on each error path.
    """
    err = SecretProviderError(message="clean error", stage="auth", status=401)
    assert_no_token_leak(str(err))
    assert_no_token_leak(repr(err))


def test_secret_provider_error_repr_does_not_leak_token() -> None:
    """HC-D.2: repr(SecretProviderError) must not inject the token itself."""
    err = SecretProviderError(message="openbao auth failed", stage="auth", status=401)
    assert_no_token_leak(repr(err))
    assert_no_token_leak(str(err))


def test_error_does_not_leak_token_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-D.2 + HC-D.3: SecretProviderError from 401 contains no token."""
    # Patch requests.get to return a 401
    def fake_get(url, *, headers, timeout, **kwargs):
        class FakeResponse:
            status_code = 401
            def json(self):
                return {"errors": ["permission denied"]}
            def raise_for_status(self):
                pass
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws")

    exc = exc_info.value
    assert_no_token_leak(str(exc))
    assert_no_token_leak(repr(exc))
    for arg in exc.args:
        assert_no_token_leak(str(arg))


def test_error_does_not_leak_token_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-D.2 + HC-D.3: SecretProviderError from 403 contains no token."""
    def fake_get(url, *, headers, timeout, **kwargs):
        class FakeResponse:
            status_code = 403
            def json(self):
                return {"errors": ["forbidden"]}
            def raise_for_status(self):
                pass
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws")

    exc = exc_info.value
    assert_no_token_leak(str(exc))
    assert_no_token_leak(repr(exc))
    for arg in exc.args:
        assert_no_token_leak(str(arg))


def test_error_does_not_leak_token_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-D.2 + HC-D.3: SecretProviderError from 500 contains no token."""
    def fake_get(url, *, headers, timeout, **kwargs):
        class FakeResponse:
            status_code = 500
            def json(self):
                return {"errors": ["internal error"]}
            def raise_for_status(self):
                pass
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws")

    exc = exc_info.value
    assert_no_token_leak(str(exc))
    assert_no_token_leak(repr(exc))
    for arg in exc.args:
        assert_no_token_leak(str(arg))


def test_error_does_not_leak_token_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-D.3: Timeout exception — 'from None' suppresses context chain."""
    def bomb(*args, **kwargs):
        raise requests.exceptions.Timeout("timeout with headers X-Vault-Token: " + SENTINEL_TOKEN)

    monkeypatch.setattr(requests, "get", bomb)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws")

    exc = exc_info.value
    assert_no_token_leak(str(exc))
    assert_no_token_leak(repr(exc))
    for arg in exc.args:
        assert_no_token_leak(str(arg))
    # I5: pin the explicit `from None` semantics (sets __cause__ to None AND
    # __suppress_context__ to True). Matches the connection_refused test
    # below; the weaker OR-form would silently pass if `from None` regressed.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_error_does_not_leak_token_on_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-D.3: ConnectionError — from None suppresses context chain."""
    def bomb(*args, **kwargs):
        raise requests.exceptions.ConnectionError(
            "connection refused — headers: " + SENTINEL_TOKEN
        )

    monkeypatch.setattr(requests, "get", bomb)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws")

    exc = exc_info.value
    assert_no_token_leak(str(exc))
    assert_no_token_leak(repr(exc))
    for arg in exc.args:
        assert_no_token_leak(str(arg))
    # I5: also assert __suppress_context__ for stronger guarantee
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_error_does_not_leak_token_on_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """HC-D.2: parse error — exception message contains no token."""
    def fake_get(url, *, headers, timeout, **kwargs):
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"unexpected": "shape"}
            def raise_for_status(self):
                pass
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws")

    exc = exc_info.value
    assert_no_token_leak(str(exc))
    assert_no_token_leak(repr(exc))
    for arg in exc.args:
        assert_no_token_leak(str(arg))


def test_safe_url_for_error_strips_query_and_fragment() -> None:
    """HC-D.4: _safe_url_for_error returns base+path without query/fragment."""
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    # Invoke the internal helper directly to verify its contract
    safe = provider._safe_url_for_error("ws-test")
    # Must not contain query string or fragment
    assert "?" not in safe
    assert "#" not in safe
    # Must contain endpoint and path
    assert "openbao.internal" in safe
    assert "ws-test" in safe


def test_safe_url_for_error_excludes_embedded_credentials() -> None:
    """HC-D.4: _safe_url_for_error strips embedded user:pass from URL."""
    # Construct a provider where the endpoint somehow has embedded creds
    # (unusual, but the helper must be defensive)
    provider = OpenBaoSecretProvider(
        endpoint="https://openbao.internal:8200",
        token="tok",
    )
    # Override endpoint to simulate a URL with embedded credentials
    # (belt+suspenders test — adapter never creates such URLs)
    provider._endpoint = "https://user:pass@openbao.internal:8200"
    safe = provider._safe_url_for_error("ws-test")
    assert "user:pass" not in safe
    assert "pass" not in safe


def test_safe_url_for_error_handles_ipv6_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """I1: IPv6 host literal is preserved with brackets in error messages."""
    provider = OpenBaoSecretProvider(
        endpoint="https://[::1]:8200",
        token=SENTINEL_TOKEN,
    )
    safe = provider._safe_url_for_error("ws-test")
    # IPv6 literal must be bracketed per RFC 3986 §3.2.2
    assert "[::1]:8200" in safe
    # Not stripped to bare "::1"
    assert "://::1" not in safe

    # Also verify via a real error path — trigger a connection error so the
    # safe URL is materialised into an actual SecretProviderError message.
    def bomb(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", bomb)
    with pytest.raises(SecretProviderError) as exc_info:
        provider.get_secrets("ws-test")
    assert "[::1]:8200" in str(exc_info.value)


# ── C1: workspace_id validation at adapter boundary ──────────────────────────


@pytest.mark.parametrize(
    "bad_workspace_id",
    [
        "../escape",
        "..",
        ".",
        "../../../etc/passwd",
        "foo/../bar",
        "a/b",
        "a\\b",
    ],
)
def test_get_secrets_rejects_path_traversal_workspace_id(
    bad_workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: path-traversal workspace_id → ValueError before any HTTP call."""
    def bomb(*args, **kwargs):
        raise AssertionError("requests.get must not be called for invalid workspace_id")

    monkeypatch.setattr(requests, "get", bomb)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(ValueError, match="workspace_id"):
        provider.get_secrets(bad_workspace_id)


@pytest.mark.parametrize(
    "bad_workspace_id",
    [
        "foo#hash",
        "foo?q=x",
        "foo\nbar",
        "foo bar",
        "",
        "foo@bar",
        "foo%2Fbar",
        "foo:bar",
    ],
)
def test_get_secrets_rejects_url_unsafe_workspace_id(
    bad_workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: URL-unsafe workspace_id → ValueError before any HTTP call."""
    def bomb(*args, **kwargs):
        raise AssertionError("requests.get must not be called for invalid workspace_id")

    monkeypatch.setattr(requests, "get", bomb)
    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    with pytest.raises(ValueError, match="workspace_id"):
        provider.get_secrets(bad_workspace_id)


@pytest.mark.parametrize(
    "good_workspace_id",
    ["alpha", "ws-prod", "workspace_1", "a.b", "a-b-c", "ABC123", "x"],
)
def test_get_secrets_accepts_valid_workspace_id(good_workspace_id: str) -> None:
    """C1: well-formed workspace_id passes validation (404 mock to avoid real HTTP)."""
    with responses_lib.RequestsMock() as rsps:
        rsps.add(
            responses_lib.GET,
            f"https://openbao.internal:8200/v1/secret/data/claw/workspaces/{good_workspace_id}",
            status=404,
            json={"errors": []},
        )
        provider = OpenBaoSecretProvider(**VALID_KWARGS)
        result = provider.get_secrets(good_workspace_id)
        assert result == {}


def test_get_secrets_validation_matches_engine_regex() -> None:
    """C1: adapter regex matches engine ``_validate_workspace_id`` exactly.

    Cross-check by importing the engine validator and confirming both raise
    on the same inputs. This guards against regex drift between adapter and
    engine.
    """
    from claw_engine.engine.context.workspace import (
        InvalidWorkspaceId,
        _validate_workspace_id,
    )

    provider = OpenBaoSecretProvider(**VALID_KWARGS)
    # Both should reject these
    for bad in ["..", ".", "../escape", "a/b", "foo bar", "foo#x", ""]:
        engine_raised = False
        try:
            _validate_workspace_id(bad)
        except InvalidWorkspaceId:
            engine_raised = True

        adapter_raised = False
        try:
            # Use a faked-out client so we can't actually reach HTTP if validation slipped
            provider.get_secrets(bad)
        except ValueError:
            adapter_raised = True
        except Exception:
            # Any other exception (e.g. AttributeError) means validation slipped
            adapter_raised = False

        assert engine_raised == adapter_raised, (
            f"divergence on {bad!r}: engine={engine_raised}, adapter={adapter_raised}"
        )


# ── Cross-impl contract: InMemory + OpenBao both satisfy SecretProvider ──────


@responses_lib.activate
def test_cross_impl_contract_happy_path() -> None:
    """Invariant 19: OpenBao provider satisfies the same happy-path contract as InMemory."""
    from claw_engine.engine.context.secrets import InMemorySecretProvider

    # InMemory baseline
    in_memory = InMemorySecretProvider(
        secrets={"ws-a": {"FOO": "bar"}}
    )
    assert in_memory.get_secrets("ws-a") == {"FOO": "bar"}

    # OpenBao via fake HTTP
    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/ws-a",
        status=200,
        json={"data": {"data": {"FOO": "bar"}, "metadata": {}}},
    )
    openbao = OpenBaoSecretProvider(**VALID_KWARGS)
    result = openbao.get_secrets("ws-a")
    assert result == {"FOO": "bar"}


@responses_lib.activate
def test_cross_impl_contract_missing_workspace() -> None:
    """Invariant 19: missing workspace returns {} in both InMemory and OpenBao."""
    from claw_engine.engine.context.secrets import InMemorySecretProvider

    in_memory = InMemorySecretProvider()
    assert in_memory.get_secrets("unknown") == {}

    responses_lib.add(
        responses_lib.GET,
        "https://openbao.internal:8200/v1/secret/data/claw/workspaces/unknown",
        status=404,
        json={"errors": []},
    )
    openbao = OpenBaoSecretProvider(**VALID_KWARGS)
    result = openbao.get_secrets("unknown")
    assert result == {}


"""
Integration tests deferred to follow-up PR.

The fake HTTP tests above (using `responses` library) are mandatory and fully cover the
adapter's HTTP semantics. Integration tests using testcontainers would spin up a real
OpenBao container and validate that the fake matches reality.

Deferred because:
1. testcontainers is a heavy dependency not needed for CI correctness.
2. The adapter's URL composition, header handling, and error taxonomy are fully verified
   by fake HTTP tests.
3. A follow-up PR can add tests/integration/test_openbao_secret_integration.py with
   @pytest.mark.integration and pytest -m integration gating.

When adding integration tests:
- pip install testcontainers
- Use @pytest.mark.integration
- pytest.importorskip("testcontainers") at module level
- Test cases: end-to-end happy path, 404 → {}, auth failure → SecretProviderError
"""
