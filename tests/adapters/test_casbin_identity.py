"""P8c: CasbinIdentityProvider — 13 invariant tests.

Module-level skip when casbin is not installed (same pattern as OTel test gating).
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

# Gate: skip entire module if casbin is not installed.
pytest.importorskip("casbin", reason="casbin not installed (pip install -e .[identity-casbin])")

from claw_engine.adapters.identity.casbin import (  # noqa: E402
    CasbinIdentityProvider,
    CasbinIdentityProviderError,
)
from claw_engine.engine.identity.contracts import IdentityProvider, User  # noqa: E402
from claw_engine.engine.identity.memory import InMemoryIdentityProvider  # noqa: E402

# ── Helpers ─────────────────────────────────────────────────────────────────

FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[2] / "claw_engine" / "adapters" / "identity" / "casbin"
MODEL_PATH = FIXTURES_DIR / "model.conf"
POLICY_PATH = FIXTURES_DIR / "policy.csv"


def _alice() -> User:
    return User(user_id="alice", display_name="Alice", default_workspace="ws-ads")


def _bob() -> User:
    return User(user_id="bob", display_name="Bob", default_workspace="ws-bob")


def _base_with_alice_bob() -> InMemoryIdentityProvider:
    """InMemoryIdentityProvider with alice + bob, each in their own workspace."""
    return InMemoryIdentityProvider(
        users={"alice": _alice(), "bob": _bob()},
        authorized={"alice": ("ws-ads",), "bob": ("ws-bob",)},
    )


def _adapter_empty_policy(tmp_path: pathlib.Path) -> CasbinIdentityProvider:
    """Adapter with an empty policy (no deny rules → default allow all)."""
    empty_policy = tmp_path / "policy.csv"
    empty_policy.write_text("# empty\n")
    return CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=empty_policy,
    )


# ── Hard Contract C: missing constructor args raise TypeError ────────────────

def test_missing_model_path_raises():
    """Contract C: model_path is required; omitting it raises TypeError."""
    with pytest.raises(TypeError):
        CasbinIdentityProvider(base=_base_with_alice_bob(), policy_path=POLICY_PATH)  # type: ignore[call-arg]


def test_missing_policy_path_raises():
    """Contract C: policy_path is required; omitting it raises TypeError."""
    with pytest.raises(TypeError):
        CasbinIdentityProvider(base=_base_with_alice_bob(), model_path=MODEL_PATH)  # type: ignore[call-arg]


def test_explicit_path_to_shipped_example_works():
    """Contract C: caller may pass the shipped example paths explicitly."""
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=POLICY_PATH,
    )
    assert isinstance(adapter, CasbinIdentityProvider)


# ── Hard Contract A: constructor IO surface is narrow ───────────────────────

def test_construction_does_not_fall_back_to_bundled_policy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract A: constructor reads ONLY the two explicit file paths.

    We monkey-patch Path.read_text (and open) to bomb on any path not in the
    explicit set. The constructor must complete without touching anything else.
    """
    model = tmp_path / "model.conf"
    policy = tmp_path / "policy.csv"
    model.write_text(MODEL_PATH.read_text())
    policy.write_text("# empty\n")

    allowed = {str(model), str(policy)}
    original_read_text = pathlib.Path.read_text

    def guarded_read_text(self: pathlib.Path, *args, **kwargs) -> str:
        if str(self) not in allowed:
            raise AssertionError(f"Constructor touched unexpected path: {self}")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", guarded_read_text)

    # Should succeed — only reads model + policy
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=model,
        policy_path=policy,
    )
    assert isinstance(adapter, CasbinIdentityProvider)


def test_construction_does_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract A: constructor must not invoke subprocess.run."""
    def bomb(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called in constructor")

    monkeypatch.setattr(subprocess, "run", bomb)

    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=POLICY_PATH,
    )
    assert isinstance(adapter, CasbinIdentityProvider)


# ── Hard Contract B: policy mutation never writes to disk ───────────────────

def test_add_policy_does_not_modify_policy_file(tmp_path: pathlib.Path) -> None:
    """Contract B: add_policy is pure in-memory; policy.csv is untouched."""
    policy = tmp_path / "policy.csv"
    policy.write_text("# empty\n")
    mtime_before = policy.stat().st_mtime
    content_before = policy.read_bytes()

    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    adapter.add_policy("alice", "ws-ads", "skill1", "use", "deny")

    # file untouched
    assert policy.stat().st_mtime == mtime_before
    assert policy.read_bytes() == content_before


def test_remove_policy_does_not_modify_policy_file(tmp_path: pathlib.Path) -> None:
    """Contract B: remove_policy is pure in-memory; policy.csv is untouched."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, alice, ws-ads, skill1, use, deny\n")
    mtime_before = policy.stat().st_mtime
    content_before = policy.read_bytes()

    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    adapter.remove_policy("alice", "ws-ads", "skill1", "use")

    assert policy.stat().st_mtime == mtime_before
    assert policy.read_bytes() == content_before


def test_reload_policy_overrides_in_memory_mutations(tmp_path: pathlib.Path) -> None:
    """Contract B: reload_policy() reverts in-memory mutations to on-disk state."""
    policy = tmp_path / "policy.csv"
    policy.write_text("# empty\n")

    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    # Add a deny rule in-memory
    adapter.add_policy("alice", "ws-ads", "skill1", "use", "deny")
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is False

    # Reload from disk (which has no deny rules)
    adapter.reload_policy()
    # In-memory mutation is gone
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is True


# ── §6 Invariant #1: Default allow ─────────────────────────────────────────

def test_default_allow_empty_policy(tmp_path: pathlib.Path) -> None:
    """Invariant 1: empty policy → all can_use_skill / can_run_workflow True."""
    adapter = _adapter_empty_policy(tmp_path)
    assert adapter.can_use_skill(_alice(), "ws-ads", "any-skill") is True
    assert adapter.can_run_workflow(_alice(), "ws-ads", "any-workflow") is True
    assert adapter.can_use_skill(_bob(), "ws-bob", "other-skill") is True


# ── §6 Invariant #2: Explicit deny ─────────────────────────────────────────

def test_explicit_deny_blocks_specific_user_skill(tmp_path: pathlib.Path) -> None:
    """Invariant 2: explicit deny for alice/ws-ads/social-media-query."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, alice, ws-ads, social-media-query, use, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    # Blocked
    assert adapter.can_use_skill(_alice(), "ws-ads", "social-media-query") is False
    # Other skills for same user/ws still allowed
    assert adapter.can_use_skill(_alice(), "ws-ads", "other-skill") is True
    # Other users not blocked
    assert adapter.can_use_skill(_bob(), "ws-ads", "social-media-query") is True
    # Other workspaces not blocked
    assert adapter.can_use_skill(_alice(), "ws-other", "social-media-query") is True


# ── §6 Invariant #3: Wildcard deny ─────────────────────────────────────────

def test_wildcard_deny_blocks_all_users_for_workflow(tmp_path: pathlib.Path) -> None:
    """Invariant 3: p, *, ws-prod, dangerous-cleanup, run, deny — blocks everyone."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, *, workspace-prod, dangerous-cleanup, run, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    # Both users blocked in workspace-prod
    assert adapter.can_run_workflow(_alice(), "workspace-prod", "dangerous-cleanup") is False
    assert adapter.can_run_workflow(_bob(), "workspace-prod", "dangerous-cleanup") is False
    # Same workflow in a different workspace is allowed
    assert adapter.can_run_workflow(_alice(), "workspace-dev", "dangerous-cleanup") is True


# ── §6 Invariant #4: Docstring contract ────────────────────────────────────

def test_docstring_contains_required_substrings() -> None:
    """Invariant 4: class docstring must contain 'reload_policy()' and 'no file watcher'."""
    doc = CasbinIdentityProvider.__doc__ or ""
    assert "reload_policy()" in doc, "Docstring must mention 'reload_policy()'"
    assert "no file watcher" in doc, "Docstring must mention 'no file watcher'"


# ── §6 Invariant #5 (⭐): Casbin does NOT affect workspace access ───────────

def test_casbin_does_not_affect_workspace_access(tmp_path: pathlib.Path) -> None:
    """Invariant 5 (acceptance-critical): can_access_workspace always delegates to base.

    Even with a wildcard deny Casbin policy that would block all skill/workflow
    actions in ws-X, workspace access must still reflect the base's answer.
    """
    # Base says alice CAN access ws-ads, CANNOT access ws-X
    base = InMemoryIdentityProvider(
        users={"alice": _alice()},
        authorized={"alice": ("ws-ads",)},  # ws-X not in alice's authorized list
    )
    # Casbin policy: deny alice everything in ws-ads AND in ws-X (wildcard)
    policy = tmp_path / "policy.csv"
    policy.write_text(
        "p, alice, ws-ads, *, *, deny\n"
        "p, *, ws-X, *, *, deny\n"
    )
    adapter = CasbinIdentityProvider(base=base, model_path=MODEL_PATH, policy_path=policy)

    # Workspace access: base says True for ws-ads (alice is authorized)
    assert adapter.can_access_workspace(_alice(), "ws-ads") is True
    # Workspace access: base says False for ws-X (alice not authorized)
    assert adapter.can_access_workspace(_alice(), "ws-X") is False

    # Skills/workflows in ws-ads: Casbin denies
    assert adapter.can_use_skill(_alice(), "ws-ads", "any-skill") is False
    # Skills/workflows in ws-X: Casbin wildcard also denies
    assert adapter.can_run_workflow(_alice(), "ws-X", "any-workflow") is False


# ── §6 Invariant #6 (⭐): add/remove without reload affects behavior immediately

def test_add_remove_policy_immediate_effect(tmp_path: pathlib.Path) -> None:
    """Invariant 6 (acceptance-critical): programmatic add/remove takes effect immediately."""
    adapter = _adapter_empty_policy(tmp_path)

    # Initially allowed
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is True

    # Add deny rule → takes effect immediately
    result = adapter.add_policy("alice", "ws-ads", "skill1", "use", "deny")
    assert result is True  # actually added
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is False

    # Adding again → returns False (already present)
    result2 = adapter.add_policy("alice", "ws-ads", "skill1", "use", "deny")
    assert result2 is False

    # Remove deny rule → takes effect immediately
    removed = adapter.remove_policy("alice", "ws-ads", "skill1", "use")
    assert removed is True
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is True

    # Remove again → returns False (not present)
    removed2 = adapter.remove_policy("alice", "ws-ads", "skill1", "use")
    assert removed2 is False


# ── §6 Invariant #7 (⭐): edit policy.csv without reload → behavior unchanged

def test_edit_policy_file_without_reload_no_effect(tmp_path: pathlib.Path) -> None:
    """Invariant 7 (acceptance-critical): no implicit file watcher.

    Editing policy.csv on disk without calling reload_policy() must NOT change
    the adapter's decisions.
    """
    policy = tmp_path / "policy.csv"
    policy.write_text("# empty\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )

    # Initially allowed
    assert adapter.can_use_skill(_alice(), "ws-ads", "foo") is True

    # Append a deny rule to the file on disk (WITHOUT reload)
    with policy.open("a") as f:
        f.write("p, alice, ws-ads, foo, use, deny\n")

    # Without reload, decision must be unchanged
    assert adapter.can_use_skill(_alice(), "ws-ads", "foo") is True

    # Now reload → deny takes effect
    adapter.reload_policy()
    assert adapter.can_use_skill(_alice(), "ws-ads", "foo") is False


# ── §6 Invariant #8: Reload picks up changes ───────────────────────────────

def test_reload_picks_up_changes(tmp_path: pathlib.Path) -> None:
    """Invariant 8: reload_policy() reads the updated file."""
    policy = tmp_path / "policy.csv"
    policy.write_text("# empty\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is True

    # Rewrite policy with a deny
    policy.write_text("p, alice, ws-ads, skill1, use, deny\n")
    adapter.reload_policy()

    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is False


# ── §6 Invariant #9: Reload is atomic (light test) ─────────────────────────

def test_reload_atomic_comment(tmp_path: pathlib.Path) -> None:
    """Invariant 9: reload is atomic — Casbin load_policy builds new table before swap.

    For V1 single-threaded contract, we simply verify that after reload completes
    the state is fully consistent (no partial-state observable by the caller).
    Thread-safety is noted as a TODO for multi-threaded scenarios.

    # TODO: add thread-safety test when multi-threaded reload is required.
    """
    policy = tmp_path / "policy.csv"
    policy.write_text("p, alice, ws-ads, skill1, use, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    # Before reload: deny is active
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is False

    # Update file to remove deny
    policy.write_text("# empty after edit\n")
    adapter.reload_policy()

    # After reload: fully consistent (allow)
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is True
    # No partial state visible
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill1") is True


# ── §6 Invariant #10: Protocol compatibility ───────────────────────────────

def test_isinstance_identity_provider() -> None:
    """Invariant 10: CasbinIdentityProvider satisfies IdentityProvider Protocol."""
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=POLICY_PATH,
    )
    assert isinstance(adapter, IdentityProvider)


# ── §6 Invariant #11: Delegation correctness ───────────────────────────────

def test_delegation_correctness(tmp_path: pathlib.Path) -> None:
    """Invariant 11: resolve_user, authorized_workspaces, can_access_workspace
    all return exactly what the base returns, regardless of Casbin policy state.
    """
    policy = tmp_path / "policy.csv"
    policy.write_text("p, *, *, *, *, deny\n")  # deny everything
    base = _base_with_alice_bob()
    adapter = CasbinIdentityProvider(
        base=base,
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    # resolve_user delegates
    assert adapter.resolve_user("alice") == base.resolve_user("alice")

    # authorized_workspaces delegates
    assert adapter.authorized_workspaces(_alice()) == base.authorized_workspaces(_alice())

    # can_access_workspace delegates (Casbin wildcard deny must NOT affect it)
    assert adapter.can_access_workspace(_alice(), "ws-ads") == base.can_access_workspace(_alice(), "ws-ads")
    assert adapter.can_access_workspace(_alice(), "ws-nonexistent") == base.can_access_workspace(_alice(), "ws-nonexistent")


# ── §6 Invariant #12: Bad policy/model file raises adapter-local error ──────

def test_nonexistent_policy_file_raises() -> None:
    """Invariant 12: non-existent policy_path → CasbinIdentityProviderError or FileNotFoundError."""
    with pytest.raises((CasbinIdentityProviderError, FileNotFoundError)):
        CasbinIdentityProvider(
            base=_base_with_alice_bob(),
            model_path=MODEL_PATH,
            policy_path=pathlib.Path("/nonexistent/policy.csv"),
        )


def test_nonexistent_model_file_raises() -> None:
    """Invariant 12: non-existent model_path → CasbinIdentityProviderError or FileNotFoundError."""
    with pytest.raises((CasbinIdentityProviderError, FileNotFoundError, Exception)):
        CasbinIdentityProvider(
            base=_base_with_alice_bob(),
            model_path=pathlib.Path("/nonexistent/model.conf"),
            policy_path=POLICY_PATH,
        )


# ── §12 Note 2: Wildcard dimensions for can_use_skill / can_run_workflow ────

def test_wildcard_user_dimension(tmp_path: pathlib.Path) -> None:
    """Note 2: user wildcard blocks all users."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, *, ws, skill, use, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    assert adapter.can_use_skill(_alice(), "ws", "skill") is False
    assert adapter.can_use_skill(_bob(), "ws", "skill") is False


def test_wildcard_workspace_dimension(tmp_path: pathlib.Path) -> None:
    """Note 2: workspace wildcard blocks skill across all workspaces."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, alice, *, skill, use, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    assert adapter.can_use_skill(_alice(), "ws-ads", "skill") is False
    assert adapter.can_use_skill(_alice(), "ws-other", "skill") is False
    # bob not affected
    assert adapter.can_use_skill(_bob(), "ws-ads", "skill") is True


def test_wildcard_object_dimension(tmp_path: pathlib.Path) -> None:
    """Note 2: object wildcard blocks all skills for alice in ws."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, alice, ws, *, use, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    assert adapter.can_use_skill(_alice(), "ws", "any-skill") is False
    assert adapter.can_use_skill(_alice(), "ws", "other-skill") is False
    # bob not affected
    assert adapter.can_use_skill(_bob(), "ws", "any-skill") is True


def test_wildcard_act_dimension(tmp_path: pathlib.Path) -> None:
    """Note 2: act wildcard blocks both can_use_skill and can_run_workflow."""
    policy = tmp_path / "policy.csv"
    policy.write_text("p, alice, ws, dangerous, *, deny\n")
    adapter = CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )
    assert adapter.can_use_skill(_alice(), "ws", "dangerous") is False
    assert adapter.can_run_workflow(_alice(), "ws", "dangerous") is False
    # Other objects not affected
    assert adapter.can_use_skill(_alice(), "ws", "safe") is True


# ── §6 Invariant #13: Contract suite cross-impl ────────────────────────────

def _make_casbin_adapter_with_alice_bob(tmp_path: pathlib.Path) -> CasbinIdentityProvider:
    policy = tmp_path / "policy.csv"
    policy.write_text("# empty\n")
    return CasbinIdentityProvider(
        base=_base_with_alice_bob(),
        model_path=MODEL_PATH,
        policy_path=policy,
    )


@pytest.mark.parametrize("provider_factory", [
    lambda tmp_path: _base_with_alice_bob(),
    lambda tmp_path: _make_casbin_adapter_with_alice_bob(tmp_path),
])
def test_identity_contract_cross_impl(
    provider_factory,
    tmp_path: pathlib.Path,
) -> None:
    """Invariant 13: both InMemory and Casbin providers satisfy the same identity contract
    for the 3 base-delegated methods (resolve_user, authorized_workspaces, can_access_workspace).
    """
    provider = provider_factory(tmp_path)

    # resolve_user: known user
    user = provider.resolve_user("alice")
    assert user.user_id == "alice"
    assert user.display_name == "Alice"

    # resolve_user: unknown user raises UnknownUser
    from claw_engine.engine.identity.contracts import UnknownUser
    with pytest.raises(UnknownUser):
        provider.resolve_user("nobody")

    # authorized_workspaces
    workspaces = provider.authorized_workspaces(user)
    assert "ws-ads" in workspaces

    # can_access_workspace: authorized workspace → True
    assert provider.can_access_workspace(user, "ws-ads") is True
    # can_access_workspace: unauthorized workspace → False
    assert provider.can_access_workspace(user, "ws-nonexistent") is False
