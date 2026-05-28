"""Adapter-local exception for OpenBaoSecretProvider failures.

Never impersonates engine exceptions. Stage taxonomy mirrors P8b's
GitSkillSourceError(stage=...) for consistency.

Token values MUST NOT appear in any default-output path:
- __str__ formats stage + status + message only.
- __repr__ formats class name + stage + status (no message body, no token).
"""
from __future__ import annotations

_VALID_STAGES = frozenset({"auth", "backend", "timeout", "network", "parse"})


class SecretProviderError(Exception):
    """Raised when OpenBaoSecretProvider cannot retrieve secrets.

    Attributes:
        stage: which phase failed — one of
               'auth', 'backend', 'timeout', 'network', 'parse'.
        status: HTTP status code (int) or None when no HTTP response was received
                (e.g. timeout, network error).
        message: human-readable description built from non-leaky fields only.
                 MUST NOT contain the OpenBao token or any request headers.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        status: int | None = None,
    ) -> None:
        if stage not in _VALID_STAGES:
            raise ValueError(
                f"stage must be one of {sorted(_VALID_STAGES)}, got {stage!r}"
            )
        # Pass message to Exception base so it appears in args[0]
        super().__init__(message)
        self.stage = stage
        self.status = status

    def __str__(self) -> str:
        status_part = f" (HTTP {self.status})" if self.status is not None else ""
        return f"[{self.stage}{status_part}] {self.args[0]}"

    def __repr__(self) -> str:
        return (
            f"SecretProviderError({self.args[0]!r}, "
            f"stage={self.stage!r}, status={self.status!r})"
        )
