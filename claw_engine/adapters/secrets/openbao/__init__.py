"""OpenBao / Vault KV v2 secret provider adapter.

Public surface:
  - ``OpenBaoSecretProvider``: read-only adapter implementing the engine's
    ``SecretProvider`` Protocol.
  - ``SecretProviderError``: adapter-local exception with ``stage`` + ``status``
    fields. Stage values: ``'auth'``, ``'backend'``, ``'timeout'``,
    ``'network'``, ``'parse'``.
"""
from __future__ import annotations

from claw_engine.adapters.secrets.openbao.errors import SecretProviderError
from claw_engine.adapters.secrets.openbao.provider import OpenBaoSecretProvider

__all__ = ["OpenBaoSecretProvider", "SecretProviderError"]
