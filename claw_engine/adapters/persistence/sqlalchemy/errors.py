"""Adapter-local exception for SQLAlchemySessionStore failures.

Never impersonates engine exceptions. Stage taxonomy:
  - 'init'   : schema creation / engine initialization failed
  - 'query'  : a read/write SQL operation failed unexpectedly
"""
from __future__ import annotations


class SessionStoreError(Exception):
    """Raised when SQLAlchemySessionStore encounters an unrecoverable error.

    Attributes:
        stage: which phase failed — one of 'init', 'query'.
        message: human-readable description; MUST NOT contain credentials.
    """

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage

    def __str__(self) -> str:
        stage_part = f"[{self.stage}] " if self.stage else ""
        return f"{stage_part}{self.args[0]}"

    def __repr__(self) -> str:
        return f"SessionStoreError({self.args[0]!r}, stage={self.stage!r})"
