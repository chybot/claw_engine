"""Adapter-local error types for CasbinIdentityProvider.

These errors are raised by the adapter itself and do NOT impersonate engine
exceptions (UnknownUser, WorkspaceAccessDenied, etc.).
"""
from __future__ import annotations


class CasbinIdentityProviderError(RuntimeError):
    """Raised when CasbinIdentityProvider cannot initialise or load a policy.

    Examples:
    - Policy or model file not found.
    - Malformed model.conf or policy.csv that Casbin cannot parse.

    This error type is adapter-local so callers that catch it do not need
    to import from the casbin package itself.
    """
