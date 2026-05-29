"""SQLAlchemy Core SessionStore adapter.

Public surface:
  - ``SQLAlchemySessionStore``: read-write adapter implementing the engine's
    ``SessionStore`` Protocol (sqlite / mysql / postgresql backends).
  - ``SessionStoreError``: adapter-local exception with ``stage`` field.
    Stage values: ``'init'``, ``'query'``.
"""
from __future__ import annotations

from claw_engine.adapters.persistence.sqlalchemy.errors import SessionStoreError
from claw_engine.adapters.persistence.sqlalchemy.store import SQLAlchemySessionStore

__all__ = ["SQLAlchemySessionStore", "SessionStoreError"]
