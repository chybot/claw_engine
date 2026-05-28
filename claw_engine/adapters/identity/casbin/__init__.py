"""CasbinIdentityProvider — Casbin-backed skill/workflow authorization adapter.

Requires the ``identity-casbin`` optional extra::

    pip install -e ".[identity-casbin]"

Usage::

    from claw_engine.adapters.identity.casbin import CasbinIdentityProvider

See :mod:`claw_engine.adapters.identity.casbin.provider` for full design
documentation.
"""
from claw_engine.adapters.identity.casbin.provider import CasbinIdentityProvider
from claw_engine.adapters.identity.casbin.errors import CasbinIdentityProviderError

__all__ = ["CasbinIdentityProvider", "CasbinIdentityProviderError"]
