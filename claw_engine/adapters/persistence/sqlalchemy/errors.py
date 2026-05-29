"""Adapter-local exception for SQLAlchemySessionStore failures.

Never impersonates engine exceptions. Stage taxonomy:
  - 'init'   : schema creation / engine initialization failed
  - 'query'  : a read/write SQL operation failed unexpectedly
"""
from __future__ import annotations

_VALID_STAGES: frozenset[str] = frozenset({"init", "query"})


class SessionStoreError(Exception):
    """Raised when SQLAlchemySessionStore encounters an unrecoverable error.

    Attributes:
        stage: which phase failed — one of 'init', 'query', or None.
        message: human-readable description; MUST NOT contain credentials.
    """

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        if stage is not None and stage not in _VALID_STAGES:
            raise ValueError(
                f"stage must be one of {sorted(_VALID_STAGES)} or None, "
                f"got {stage!r}"
            )
        super().__init__(message)
        self.stage = stage

    def __str__(self) -> str:
        stage_part = f"[{self.stage}] " if self.stage else ""
        return f"{stage_part}{self.args[0]}"

    def __repr__(self) -> str:
        return f"SessionStoreError({self.args[0]!r}, stage={self.stage!r})"
