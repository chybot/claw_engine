"""CasbinIdentityProvider — Casbin-backed skill/workflow authorization adapter.

This adapter *wraps* an existing ``IdentityProvider`` (the "base") and routes
``can_use_skill`` / ``can_run_workflow`` decisions through a Casbin enforcer
while delegating ``resolve_user``, ``authorized_workspaces``, and
``can_access_workspace`` verbatim to the base.

Design decisions
----------------
- **Composition, not subclassing.** Accepting any ``IdentityProvider``-conforming
  base means the adapter is interchangeable regardless of which engine-side
  provider backs the user/workspace data.

- **Default-allow + explicit-deny.** The Casbin model uses::

      e = !some(where (p.eft == deny))

  This mirrors V1 ``InMemoryIdentityProvider.denied_skills`` semantics: every
  skill/workflow is allowed unless a deny rule matches.

- **Flat policy table.** No group/role inheritance in V1. If user X needs the
  same denies as user Y, write them separately in policy.csv.

- **Workspace-access is 100% base-delegated.** Casbin is NEVER consulted for
  ``can_access_workspace``. Even a wildcard deny policy like
  ``p, *, ws-X, *, *, deny`` has no effect on workspace access decisions.

- **Constructor IO.** ``__init__`` performs exactly one small IO operation:
  loading ``model_path`` and ``policy_path`` into memory via Casbin.
  No network calls, no subprocesses, no filesystem scans, no file watchers.

- **Policy reload semantics.** Edits to policy.csv on disk DO NOT take effect
  until ``reload_policy()`` is called or the process is restarted. There is
  no file watcher. The three explicit control points are:

  * ``reload_policy()`` — re-reads policy.csv from disk (atomic swap).
  * ``add_policy(...)`` — adds an in-memory rule. Does NOT write to disk.
  * ``remove_policy(...)`` — removes an in-memory rule. Does NOT write to disk.

  V1 does not implement ``save_policy()``. To persist in-memory changes, edit
  policy.csv manually and call ``reload_policy()``.

Usage example
-------------
::

    from claw_engine.engine.identity.memory import InMemoryIdentityProvider
    from claw_engine.adapters.identity.casbin import CasbinIdentityProvider

    base = InMemoryIdentityProvider(users={"alice": ...}, authorized={"alice": (...)})
    identity = CasbinIdentityProvider(
        base=base,
        model_path="path/to/model.conf",
        policy_path="path/to/policy.csv",
    )
    # identity satisfies the engine IdentityProvider Protocol.
"""
from __future__ import annotations

import pathlib
from typing import Union

import casbin  # intentionally only imported inside this adapter sub-package

from claw_engine.engine.identity.contracts import IdentityProvider, User
from claw_engine.adapters.identity.casbin.errors import CasbinIdentityProviderError

# Allowed effect values for add_policy / remove_policy. Casbin's matcher
# ``e = !some(where (p.eft == deny))`` is case-sensitive — any other casing
# (e.g. ``"Deny"``) becomes silently inert policy noise. Validating eagerly
# turns that class of typo into an obvious caller-side error.
_ALLOWED_EFFECTS: tuple[str, ...] = ("allow", "deny")


class CasbinIdentityProvider:
    """Casbin-backed skill/workflow authorization adapter.

    Wraps a base ``IdentityProvider`` and enforces skill/workflow deny rules
    using a Casbin enforcer with a default-allow + explicit-deny PERM model.

    Edits to policy.csv on disk DO NOT take effect until ``reload_policy()``
    is called or the process is restarted. There is no file watcher.

    :param base: Any ``IdentityProvider``-conforming provider. Its
        ``resolve_user``, ``authorized_workspaces``, and
        ``can_access_workspace`` answers are returned verbatim.
    :param model_path: Path to the Casbin model configuration file (required,
        no default). The shipped ``model.conf`` in this package is a documented
        example; production callers supply their own or re-use this one
        explicitly.
    :param policy_path: Path to the Casbin CSV policy file (required, no
        default). The shipped ``policy.csv`` in this package is a documented
        example; production callers supply their own.
    :raises TypeError: If ``model_path`` or ``policy_path`` are omitted.
    :raises CasbinIdentityProviderError: If the model or policy file cannot
        be loaded (e.g. file not found, malformed content).
    """

    def __init__(
        self,
        base: IdentityProvider,
        *,
        model_path: Union[pathlib.Path, str],
        policy_path: Union[pathlib.Path, str],
    ) -> None:
        self._base = base
        self._model_path = pathlib.Path(model_path)
        self._policy_path = pathlib.Path(policy_path)
        self._enforcer = self._load_enforcer(self._model_path, self._policy_path)

    # ── Internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _load_enforcer(
        model_path: pathlib.Path,
        policy_path: pathlib.Path,
    ) -> casbin.Enforcer:
        """Load a Casbin Enforcer from the given paths.

        Raises ``CasbinIdentityProviderError`` (wrapping any underlying
        Casbin / OS exception) so the adapter-local error type doesn't leak
        Casbin internals to callers.
        """
        if not model_path.exists():
            raise CasbinIdentityProviderError(
                f"Casbin model file not found: {model_path}"
            )
        if not policy_path.exists():
            raise CasbinIdentityProviderError(
                f"Casbin policy file not found: {policy_path}"
            )
        try:
            return casbin.Enforcer(str(model_path), str(policy_path))
        except Exception as exc:
            raise CasbinIdentityProviderError(
                f"Failed to load Casbin enforcer (model={model_path}, "
                f"policy={policy_path}): {exc}"
            ) from exc

    # ── IdentityProvider Protocol — base-delegated methods ──────────────────

    def resolve_user(self, raw_user_ref: str) -> User:
        """Delegate to base. Raises ``UnknownUser`` if the user is not found."""
        return self._base.resolve_user(raw_user_ref)

    def authorized_workspaces(self, user: User) -> tuple[str, ...]:
        """Delegate to base."""
        return self._base.authorized_workspaces(user)

    def can_access_workspace(self, user: User, workspace_id: str) -> bool:
        """Delegate 100% to base. Casbin is NOT consulted for workspace access.

        This is intentional: workspace-level access control (allow/deny the
        workspace itself) uses the base provider's user/workspace mapping.
        Casbin governs only skill/workflow actions within a workspace.
        """
        return self._base.can_access_workspace(user, workspace_id)

    # ── IdentityProvider Protocol — Casbin-backed methods ───────────────────

    def can_use_skill(self, user: User, workspace_id: str, skill: str) -> bool:
        """Return True unless a Casbin deny rule matches (user, workspace, skill, "use").

        Default-allow: if no rule matches, True is returned.
        """
        return bool(self._enforcer.enforce(user.user_id, workspace_id, skill, "use"))

    def can_run_workflow(self, user: User, workspace_id: str, workflow: str) -> bool:
        """Return True unless a Casbin deny rule matches (user, workspace, workflow, "run").

        Default-allow: if no rule matches, True is returned.
        """
        return bool(self._enforcer.enforce(user.user_id, workspace_id, workflow, "run"))

    # ── Adapter-local control surface (NOT in engine Protocol) ──────────────

    def reload_policy(self) -> None:
        """Re-read ``policy_path`` from disk and replace the in-memory policy atomically.

        Casbin's ``load_policy()`` builds a new internal policy table before
        swapping, so no partial-state window is observable to the caller in
        single-threaded usage.

        Any in-memory mutations made via ``add_policy`` / ``remove_policy``
        since the last construction or reload will be discarded.

        Call this after editing policy.csv on disk to pick up the changes.
        There is no file watcher — changes do NOT take effect automatically.

        :raises CasbinIdentityProviderError: If ``policy_path`` no longer exists
            on disk, or if Casbin fails to parse it. The adapter-local error
            type is preserved so callers don't have to import from casbin
            to handle reload failures.
        """
        if not self._policy_path.exists():
            raise CasbinIdentityProviderError(
                f"Casbin policy file not found: {self._policy_path}"
            )
        try:
            self._enforcer.load_policy()
        except Exception as exc:
            raise CasbinIdentityProviderError(
                f"Failed to reload Casbin policy from {self._policy_path}: {exc}"
            ) from exc

    def add_policy(
        self,
        sub: str,
        ws: str,
        obj: str,
        act: str,
        eft: str = "deny",
    ) -> bool:
        """Add a rule to the in-memory policy.

        This is a pure in-memory operation. The ``policy_path`` file on disk
        is NOT modified.

        :param sub: Subject (user_id or ``"*"`` for wildcard).
        :param ws: Workspace ID (or ``"*"``).
        :param obj: Skill or workflow name (or ``"*"``).
        :param act: Action — ``"use"``, ``"run"``, or ``"*"``.
        :param eft: Effect — ``"deny"`` (default) or ``"allow"``. Case-sensitive;
            anything else raises ``ValueError`` (avoids silent typo policies).
        :returns: True if the rule was added; False if it was already present.
        :raises ValueError: If ``eft`` is not exactly ``"allow"`` or ``"deny"``.
        """
        if eft not in _ALLOWED_EFFECTS:
            raise ValueError(
                f"eft must be one of {_ALLOWED_EFFECTS}, got {eft!r}"
            )
        return bool(self._enforcer.add_policy(sub, ws, obj, act, eft))

    def remove_policy(
        self,
        sub: str,
        ws: str,
        obj: str,
        act: str,
        eft: str = "deny",
    ) -> bool:
        """Remove a single Casbin rule matching ``(sub, ws, obj, act, eft)`` from
        the in-memory policy.

        Mirrors :meth:`add_policy`'s shape — the ``eft`` field is required to
        disambiguate when both an allow and a deny rule exist for the same
        ``(sub, ws, obj, act)`` tuple. Defaults to ``"deny"`` since deny rules
        are the dominant case under this adapter's default-allow model.

        This is a pure in-memory operation. The ``policy_path`` file on disk
        is NOT modified. Use :meth:`reload_policy` to discard in-memory
        mutations and revert to the on-disk policy.

        :param sub: Subject (user_id or ``"*"``).
        :param ws: Workspace ID (or ``"*"``).
        :param obj: Skill or workflow name (or ``"*"``).
        :param act: Action — ``"use"``, ``"run"``, or ``"*"``.
        :param eft: Effect — ``"deny"`` (default) or ``"allow"``.
        :returns: True if a rule was removed; False if no matching rule existed.
        :raises ValueError: If ``eft`` is not exactly ``"allow"`` or ``"deny"``.
        """
        if eft not in _ALLOWED_EFFECTS:
            raise ValueError(
                f"eft must be one of {_ALLOWED_EFFECTS}, got {eft!r}"
            )
        return bool(self._enforcer.remove_policy(sub, ws, obj, act, eft))
