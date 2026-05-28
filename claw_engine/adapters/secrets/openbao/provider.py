"""OpenBaoSecretProvider — KV v2 read-only adapter for OpenBao / HashiCorp Vault.

Implements the engine's ``SecretProvider`` Protocol via a plain ``requests.get``
call. Uses ``requests`` directly (no hvac); see sub-plan §4 for rationale.

Lifecycle (strict separation):
- Construction: pure-string validation only. NO HTTP.
- Read: ``get_secrets(workspace_id)`` issues one HTTP GET.

Token safety (HC-D):
- Token stored as ``self._token`` only.
- ``__repr__`` / ``__str__`` never include the token.
- ``SecretProviderError`` messages are built from non-leaky fields.
- ``requests`` exception chains suppressed with ``from None``.
"""
from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import urlparse, urlunparse

import requests
import requests.exceptions

from claw_engine.adapters.secrets.openbao.errors import SecretProviderError

# Accept header sent on every request
_ACCEPT_JSON = "application/json"

# Mirrors engine/context/workspace.py::_WORKSPACE_ID_RE and the ('.', '..')
# guard in _validate_workspace_id. Duplicated rather than imported because the
# engine 0-diff rule forbids reaching into engine internals from adapters.
# If this regex diverges across adapters, promote to a public helper in
# engine.context (deferred to a follow-up PR).
_WORKSPACE_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")


class OpenBaoSecretProvider:
    """Read-only KV v2 adapter for OpenBao / Vault.

    :param endpoint: Base URL, e.g. ``"https://openbao.internal:8200"``.
        Must start with ``http://`` or ``https://``. Trailing slash stripped.
    :param token: OpenBao token. Stored internally, never logged or repr'd.
    :param mount: KV v2 mount point (default ``"secret"``). Single path segment,
        no slashes.
    :param path_template: Path template under mount. Must contain exactly one
        ``{workspace_id}`` placeholder. Must not contain ``..``.
    :param timeout_connect: TCP connect timeout in seconds (default 5.0, positive).
    :param timeout_read: Read timeout in seconds (default 30.0, positive).
    :raises ValueError: On any invalid constructor argument.
    """

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        mount: str = "secret",
        path_template: str = "claw/workspaces/{workspace_id}",
        timeout_connect: float = 5.0,
        timeout_read: float = 30.0,
    ) -> None:
        # ── Validate endpoint ──────────────────────────────────────────────
        if not endpoint or not isinstance(endpoint, str):
            raise ValueError("endpoint must be a non-empty string")
        if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            raise ValueError(
                f"endpoint must start with 'http://' or 'https://', got {endpoint!r}"
            )
        # I2: Reject endpoints carrying credentials, query strings, or fragments.
        # Embedded credentials in endpoint would leak via __repr__ even after
        # HC-D.1 token redaction; query/fragment would break URL composition.
        _parsed_endpoint = urlparse(endpoint)
        if _parsed_endpoint.username or _parsed_endpoint.password:
            raise ValueError(
                "endpoint must not contain embedded credentials (user:pass@host); "
                "pass the OpenBao token via the `token` parameter instead"
            )
        if _parsed_endpoint.query or _parsed_endpoint.fragment:
            raise ValueError(
                f"endpoint must not contain query or fragment, got {endpoint!r}"
            )
        # ── Validate token ─────────────────────────────────────────────────
        if not token or not isinstance(token, str):
            raise ValueError("token must be a non-empty string")
        # ── Validate mount ─────────────────────────────────────────────────
        if not mount or not isinstance(mount, str):
            raise ValueError("mount must be a non-empty string")
        if "/" in mount:
            raise ValueError(
                f"mount must be a single path segment (no slashes), got {mount!r}"
            )
        # ── Validate path_template ─────────────────────────────────────────
        if not path_template or not isinstance(path_template, str):
            raise ValueError("path_template must be a non-empty string")
        if "{workspace_id}" not in path_template:
            raise ValueError(
                "path_template must contain exactly one '{workspace_id}' placeholder, "
                f"got {path_template!r}"
            )
        if ".." in path_template:
            raise ValueError(
                f"path_template must not contain '..' segments, got {path_template!r}"
            )
        # ── Validate timeouts ──────────────────────────────────────────────
        if not isinstance(timeout_connect, (int, float)) or timeout_connect <= 0:
            raise ValueError(
                f"timeout_connect must be a positive number, got {timeout_connect!r}"
            )
        if not isinstance(timeout_read, (int, float)) or timeout_read <= 0:
            raise ValueError(
                f"timeout_read must be a positive number, got {timeout_read!r}"
            )

        # ── Store (token stored on _token, never in repr) ──────────────────
        self._endpoint = endpoint.rstrip("/")
        self._token = token  # HC-D: only path to token value is self._token
        self._mount = mount
        self._path_template = path_template
        self._timeout_connect = float(timeout_connect)
        self._timeout_read = float(timeout_read)

    # ── SecretProvider Protocol ───────────────────────────────────────────────

    def get_secrets(self, workspace_id: str) -> Mapping[str, str]:
        """Fetch secrets for ``workspace_id`` from OpenBao KV v2.

        Returns an empty mapping when the path does not exist (HTTP 404).
        Raises ``SecretProviderError`` for all other non-200 responses and
        network/parse failures.

        :param workspace_id: Workspace identifier used to fill ``path_template``.
        :returns: ``Mapping[str, str]`` of secret keys to values.
        :raises ValueError: If ``workspace_id`` fails the path-traversal /
            URL-injection guard (mirrors engine ``_validate_workspace_id``).
        :raises SecretProviderError: On auth failure, backend error, timeout,
            network error, or malformed response body.
        """
        # C1: Defense-in-depth — validate at the adapter boundary even though
        # WorkspaceResolver also validates. Anyone holding a SecretProvider
        # reference can call this directly, bypassing engine validation.
        # Mirrors engine/context/workspace.py::_validate_workspace_id exactly:
        # regex r"[A-Za-z0-9_.-]+" with fullmatch + explicit ('.', '..') ban.
        if (
            not workspace_id
            or not isinstance(workspace_id, str)
            or workspace_id in (".", "..")
            or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
        ):
            raise ValueError(
                f"Invalid workspace_id (path-traversal / URL-injection guard): "
                f"{workspace_id!r}. Must match [A-Za-z0-9_.-]+ and not be '.' or '..'."
            )

        path = self._path_template.format(workspace_id=workspace_id)
        url = f"{self._endpoint}/v1/{self._mount}/data/{path}"
        headers = {
            "X-Vault-Token": self._token,
            "Accept": _ACCEPT_JSON,
        }
        timeout = (self._timeout_connect, self._timeout_read)

        # HC-D.3: Wrap requests exceptions, never include str(original_exception)
        # in the message, and always use `from None` to suppress __context__.
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except requests.exceptions.Timeout:
            raise SecretProviderError(
                message=f"OpenBao request timed out: {self._safe_url_for_error(workspace_id)}",
                stage="timeout",
                status=None,
            ) from None
        except requests.exceptions.ConnectionError:
            raise SecretProviderError(
                message=f"OpenBao connection failed: {self._safe_url_for_error(workspace_id)}",
                stage="network",
                status=None,
            ) from None

        # HC-B: 404 → empty mapping
        if response.status_code == 404:
            return {}

        # HC-C: auth errors
        if response.status_code in (401, 403):
            raise SecretProviderError(
                message=(
                    f"OpenBao authentication/authorization error at "
                    f"{self._safe_url_for_error(workspace_id)}"
                ),
                stage="auth",
                status=response.status_code,
            )

        # HC-C: backend errors (5xx)
        if response.status_code >= 500:
            raise SecretProviderError(
                message=(
                    f"OpenBao backend error (HTTP {response.status_code}) at "
                    f"{self._safe_url_for_error(workspace_id)}"
                ),
                stage="backend",
                status=response.status_code,
            )

        # Other non-200 (e.g. 400, 405) — treat as backend error
        if response.status_code != 200:
            raise SecretProviderError(
                message=(
                    f"OpenBao unexpected HTTP {response.status_code} at "
                    f"{self._safe_url_for_error(workspace_id)}"
                ),
                stage="backend",
                status=response.status_code,
            )

        # HTTP 200 — parse KV v2 body.
        # I4: narrow except to expected parse-failure types. KeyError/TypeError
        # cover missing or non-dict `data.data`; ValueError covers
        # requests.exceptions.JSONDecodeError (subclass of both ValueError and
        # requests.RequestException — listed explicitly for clarity).
        try:
            body = response.json()
            secrets_raw = body["data"]["data"]
        except (KeyError, TypeError, ValueError, requests.exceptions.JSONDecodeError):
            raise SecretProviderError(
                message=(
                    f"OpenBao response body malformed (missing data.data or "
                    f"invalid JSON) at {self._safe_url_for_error(workspace_id)}"
                ),
                stage="parse",
                status=200,
            ) from None

        # Coerce all values to str (KV v2 can have non-string JSON values)
        return {k: str(v) for k, v in secrets_raw.items()}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _safe_url_for_error(self, workspace_id: str) -> str:
        """Return a safe URL string for use in error messages (HC-D.4).

        Strips query, fragment, and embedded credentials. Only base URL + path
        are returned. Defensive even though the adapter never constructs URLs
        with query strings, fragments, or embedded credentials.

        I1: IPv6 hosts (``parsed.hostname`` returns ``"::1"`` without brackets)
        are re-wrapped in brackets so the rebuilt URL stays well-formed.
        """
        path = self._path_template.format(workspace_id=workspace_id)
        raw = f"{self._endpoint}/v1/{self._mount}/data/{path}"
        parsed = urlparse(raw)
        # Rebuild with only scheme + netloc (stripped of user:pass) + path
        host = parsed.hostname or ""
        if ":" in host:  # IPv6 literal — wrap in brackets per RFC 3986 §3.2.2
            host = f"[{host}]"
        port_part = f":{parsed.port}" if parsed.port else ""
        safe_netloc = f"{host}{port_part}"
        safe = urlunparse((parsed.scheme, safe_netloc, parsed.path, "", "", ""))
        return safe

    # ── Representation (HC-D.1: no token) ────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"OpenBaoSecretProvider("
            f"endpoint={self._endpoint!r}, "
            f"mount={self._mount!r}, "
            f"path_template={self._path_template!r}, "
            f"token=<redacted>"
            f")"
        )

    def __str__(self) -> str:
        return self.__repr__()
