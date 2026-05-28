"""Invariant tests for GitSkillSource adapter.

Covers all 15 invariants from P8b sub-plan §8.

Acceptance-critical invariants (starred):
  3  pre-refresh hermetic (monkey-patch)
  4  post-refresh hermetic (monkey-patch)
  13 path validation matrix
  15 SkillProvisioner integration

Module-level gate: skip entire module if git is not on PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Gate on system git.
if shutil.which("git") is None:
    pytest.skip("git not on PATH", allow_module_level=True)

from claw_engine.adapters.skills.git import GitSkillSource, GitSkillSourceError
from claw_engine.adapters.skills.git.source import _validate_allowed_path
from claw_engine.engine.skills.source import SkillNotFound
from claw_engine.engine.skills.provisioner import SkillProvisioner
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.identity.contracts import User
from claw_engine.engine.context.workspace import ResolvedWorkspace

from tests.contract.skillsource_fixtures import (
    make_bare_repo_with_skills,
    make_bare_repo_with_two_branches,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolved(cwd: Path, allowed: tuple[str, ...]) -> ResolvedWorkspace:
    return ResolvedWorkspace(
        workspace_id="ws1",
        cwd=str(cwd),
        env={},
        sensitive_keys=frozenset(),
        allowed_skills=allowed,
    )


def _identity(denied: tuple[str, ...] = ()) -> tuple[InMemoryIdentityProvider, User]:
    idp = InMemoryIdentityProvider(
        users={"ref-u1": User(user_id="u1", display_name="A", default_workspace="ws1")},
        authorized={"u1": ("ws1",)},
        denied_skills={"u1": denied},
    )
    return idp, idp.resolve_user("ref-u1")


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 1 — No-IO construction
# ─────────────────────────────────────────────────────────────────────────────


def test_no_io_construction(tmp_path: Path) -> None:
    """GitSkillSource(eager=False) must not raise even for a nonexistent repo."""
    # Should NOT touch the network or disk.
    gs = GitSkillSource("file:///nonexistent.git", "main", tmp_path / "cache")
    # Read methods return safe defaults — no error.
    assert gs.has_skill("x") is False
    assert gs.has_dir("x") is False
    with pytest.raises(SkillNotFound):
        gs.skill_dir("x")


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 2 — Eager construction
# ─────────────────────────────────────────────────────────────────────────────


def test_eager_construction(tmp_path: Path) -> None:
    """eager=True triggers refresh during __init__; read methods work immediately."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"foo": "# foo skill"},
    )
    gs = GitSkillSource(url, "main", tmp_path / "cache", eager=True)
    assert gs.has_skill("foo") is True


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 3 — Pre-refresh hermetic (⭐ acceptance-critical)
# ─────────────────────────────────────────────────────────────────────────────


def test_pre_refresh_read_methods_never_invoke_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BEFORE refresh: read methods must never call subprocess.run."""

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("subprocess.run must NOT be called by read methods")

    monkeypatch.setattr(subprocess, "run", _boom)

    gs = GitSkillSource("file:///irrelevant.git", "main", Path("/tmp/irrelevant_cache"))
    # These three must not reach subprocess even with the bomb installed.
    assert gs.has_skill("foo") is False
    assert gs.has_dir("foo") is False
    with pytest.raises(SkillNotFound):
        gs.skill_dir("foo")


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 4 — Post-refresh hermetic (⭐ acceptance-critical)
# ─────────────────────────────────────────────────────────────────────────────


def test_post_refresh_read_methods_never_invoke_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AFTER refresh: read methods must never call subprocess.run."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"foo": "# foo"},
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache)
    gs.refresh()  # normal refresh completes successfully

    # Now bomb subprocess.run — subsequent reads must be pure filesystem.
    call_count = {"n": 0}

    def _bomb(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        raise AssertionError("subprocess.run must NOT be called by read methods")

    monkeypatch.setattr(subprocess, "run", _bomb)

    # These must succeed without hitting the bomb.
    assert gs.has_skill("foo") is True
    assert gs.has_dir("foo") is True
    gs.skill_dir("foo")  # must not raise
    assert call_count["n"] == 0, "subprocess.run was called during read!"


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 5 — Idempotent refresh
# ─────────────────────────────────────────────────────────────────────────────


def test_idempotent_refresh(tmp_path: Path) -> None:
    """Calling refresh() twice yields the same commit SHA."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"alpha": "# alpha"},
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache)
    r1 = gs.refresh()
    r2 = gs.refresh()
    assert r1.ref == r2.ref
    assert gs.has_skill("alpha") is True


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 6 — Re-ref refresh
# ─────────────────────────────────────────────────────────────────────────────


def test_reref_refresh(tmp_path: Path) -> None:
    """refresh(ref='branch2') switches checkout after initial refresh(ref='main')."""
    url = make_bare_repo_with_two_branches(
        tmp_path / "repo",
        main_skills={"skill_on_main": "# main"},
        branch2_skills={"skill_on_b2": "# b2"},
        branch2_name="branch2",
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache)
    gs.refresh()
    assert gs.has_skill("skill_on_main") is True

    gs.refresh(ref="branch2")
    # branch2 inherits main's skills plus adds skill_on_b2
    assert gs.has_skill("skill_on_b2") is True


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 7 — allowed_skill_paths explicit list
# ─────────────────────────────────────────────────────────────────────────────


def test_allowed_paths_explicit_list(tmp_path: Path) -> None:
    """allowed_skill_paths=['foo','bar'] → only those skills materialise."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"foo": "# foo", "bar": "# bar", "baz": "# baz"},
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache, allowed_skill_paths=["foo", "bar"])
    result = gs.refresh()
    assert set(result.materialized_paths) == {"foo", "bar"}
    assert gs.has_skill("foo") is True
    assert gs.has_skill("bar") is True
    # baz was not requested — may or may not be on disk (sparse), but NOT in
    # materialized_paths; adapter only reports what it explicitly managed.
    assert "baz" not in result.materialized_paths


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 8 — allowed_skill_paths=None (default full scan)
# ─────────────────────────────────────────────────────────────────────────────


def test_allowed_paths_default_none(tmp_path: Path) -> None:
    """allowed_skill_paths=None → all three top-level skills materialise."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"alpha": "# a", "beta": "# b", "gamma": "# c"},
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache)
    result = gs.refresh()
    assert gs.has_skill("alpha") is True
    assert gs.has_skill("beta") is True
    assert gs.has_skill("gamma") is True
    assert set(result.materialized_paths) == {"alpha", "beta", "gamma"}


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 9 — Nested layout
# ─────────────────────────────────────────────────────────────────────────────


def test_nested_layout(tmp_path: Path) -> None:
    """allowed_skill_paths=['skills/algo/dag_tracer'] → has_skill('dag_tracer') is True."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"dag_tracer": "# dag tracer"},
        layout="nested",
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(
        url,
        "main",
        cache,
        allowed_skill_paths=["skills/dag_tracer"],
    )
    result = gs.refresh()
    # Skill name is the basename of the path.
    assert "dag_tracer" in result.materialized_paths
    assert gs.has_skill("dag_tracer") is True
    d = gs.skill_dir("dag_tracer")
    assert os.path.isfile(os.path.join(d, "SKILL.md"))


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 10 — Bad repo URL → stage='clone'
# ─────────────────────────────────────────────────────────────────────────────


def test_bad_repo_url_raises_clone_stage(tmp_path: Path) -> None:
    gs = GitSkillSource("file:///does/not/exist.git", "main", tmp_path / "cache")
    with pytest.raises(GitSkillSourceError) as exc_info:
        gs.refresh()
    assert exc_info.value.stage == "clone"


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 11 — Bad ref → stage in {'fetch', 'checkout'}
# ─────────────────────────────────────────────────────────────────────────────


def test_bad_ref_raises_fetch_or_checkout_stage(tmp_path: Path) -> None:
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"foo": "# foo"},
    )
    gs = GitSkillSource(url, "nonexistent-branch-xyz", tmp_path / "cache")
    with pytest.raises(GitSkillSourceError) as exc_info:
        gs.refresh()
    assert exc_info.value.stage in {"fetch", "checkout"}


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 12 — git not on PATH → stage='invoke'
# ─────────────────────────────────────────────────────────────────────────────


def test_no_git_on_path_raises_invoke_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkey-patch subprocess.run to raise FileNotFoundError (simulates no git)."""

    def _no_git(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _no_git)

    gs = GitSkillSource("file:///any.git", "main", tmp_path / "cache")
    with pytest.raises(GitSkillSourceError) as exc_info:
        gs.refresh()
    assert exc_info.value.stage == "invoke"


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 13 — Path validation matrix (⭐ acceptance-critical)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",            # absolute path
        "../etc",                 # traversal at root
        "skills/../etc",          # traversal in middle
        "skills//double",         # empty segment (double slash)
        "skills\\evil",           # backslash
        "  skills/foo",           # leading whitespace
        "skills/foo  ",           # trailing whitespace
        "a" * 257,                # oversize (> 256)
    ],
    ids=[
        "absolute_path",
        "traversal_root",
        "traversal_middle",
        "double_slash",
        "backslash",
        "leading_whitespace",
        "trailing_whitespace",
        "oversize",
    ],
)
def test_path_validation_rejects_bad_paths(
    bad_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each bad path raises GitSkillSourceError(stage='validate') BEFORE subprocess."""

    def _bomb(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("subprocess.run must NOT be called when path is invalid")

    monkeypatch.setattr(subprocess, "run", _bomb)

    with pytest.raises(GitSkillSourceError) as exc_info:
        _validate_allowed_path(bad_path)
    assert exc_info.value.stage == "validate"


@pytest.mark.parametrize(
    "good_path",
    [
        "skills/foo",
        "skills/algo/dag_tracer",
        "foo",
    ],
    ids=["nested_one", "nested_two", "flat"],
)
def test_path_validation_accepts_good_paths(good_path: str) -> None:
    """Valid paths must not raise."""
    _validate_allowed_path(good_path)  # must not raise


def test_path_validation_rejects_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure refresh() calls validation before any subprocess invocation."""

    def _bomb(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("subprocess must NOT be called before path validation rejects")

    monkeypatch.setattr(subprocess, "run", _bomb)

    gs = GitSkillSource(
        "file:///any.git",
        "main",
        tmp_path / "cache",
        allowed_skill_paths=["/etc/passwd"],  # invalid
    )
    with pytest.raises(GitSkillSourceError) as exc_info:
        gs.refresh()
    assert exc_info.value.stage == "validate"


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 14 — cache_dir realpath containment
# ─────────────────────────────────────────────────────────────────────────────


def test_cache_dir_realpath_containment(tmp_path: Path) -> None:
    """All files in cache_dir resolve under cache_dir.realpath() — no symlinks escape."""
    url = make_bare_repo_with_skills(
        tmp_path / "repo",
        skills={"foo": "# foo"},
    )
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache)
    gs.refresh()

    cache_real = os.path.realpath(cache)
    for dirpath, _dirs, files in os.walk(cache_real):
        for fname in files:
            full = os.path.join(dirpath, fname)
            real = os.path.realpath(full)
            common = os.path.commonpath([cache_real, real])
            assert common == cache_real, (
                f"File {full!r} resolves to {real!r} which is outside cache_dir"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 15 — SkillProvisioner integration (⭐ acceptance-critical)
# ─────────────────────────────────────────────────────────────────────────────


def test_provisioner_integration_full(tmp_path: Path) -> None:
    """Full Provisioner integration: provision, deny, revoke, invalid bucket."""

    # --- Phase 1: build repo with 'foo' and 'broken' (no SKILL.md). ---
    repo_base = tmp_path / "repo"
    repo_base.mkdir()
    bare = repo_base / "repo.git"
    work = repo_base / "work"

    def _r(*args: str, cwd: Path) -> None:
        result = subprocess.run(list(args), cwd=str(cwd), check=False,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{args}: {result.stderr}")

    _r("git", "init", "--bare", str(bare), cwd=repo_base)
    _r("git", "clone", f"file://{bare}", str(work), cwd=repo_base)
    _r("git", "config", "user.email", "t@t.com", cwd=work)
    _r("git", "config", "user.name", "T", cwd=work)

    # 'foo' — valid skill
    (work / "foo").mkdir()
    (work / "foo" / "SKILL.md").write_text("# foo", encoding="utf-8")
    # 'broken' — dir with no SKILL.md (invalid bucket)
    (work / "broken").mkdir()
    (work / "broken" / "run.py").write_text("# no SKILL.md", encoding="utf-8")

    _r("git", "add", ".", cwd=work)
    _r("git", "commit", "-m", "init", cwd=work)
    _r("git", "push", "origin", "HEAD:main", cwd=work)

    url = f"file://{bare}"
    cache = tmp_path / "cache"
    gs = GitSkillSource(url, "main", cache)
    gs.refresh()

    idp, user = _identity()
    provisioner = SkillProvisioner(gs, idp)

    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    rw = _resolved(ws_dir, ("foo", "broken"))

    result = provisioner.provision(rw, user)

    # 'foo' provisioned to disk.
    assert "foo" in result.provisioned
    skill_path = ws_dir / ".agents" / "skills" / "foo" / "SKILL.md"
    assert skill_path.is_file(), f"Expected {skill_path} to exist"

    # 'broken' has no SKILL.md → invalid bucket.
    assert "broken" in result.invalid

    # --- Phase 2: denied skill via identity. ---
    idp2, user2 = _identity(denied=("foo",))
    provisioner2 = SkillProvisioner(gs, idp2)

    ws_dir2 = tmp_path / "workspace2"
    ws_dir2.mkdir()
    rw2 = _resolved(ws_dir2, ("foo",))
    result2 = provisioner2.provision(rw2, user2)

    assert "foo" in result2.denied
    denied_path = ws_dir2 / ".agents" / "skills" / "foo"
    assert not denied_path.exists(), "Denied skill must not be materialized"

    # --- Phase 3: revocation — remove 'foo' from the repo. ---
    # Push a new commit removing foo.
    work2 = repo_base / "work2"
    _r("git", "clone", f"file://{bare}", str(work2), cwd=repo_base)
    _r("git", "config", "user.email", "t@t.com", cwd=work2)
    _r("git", "config", "user.name", "T", cwd=work2)

    import shutil as _shutil
    _shutil.rmtree(work2 / "foo")

    _r("git", "add", ".", cwd=work2)
    _r("git", "commit", "-m", "remove foo", cwd=work2)
    _r("git", "push", "origin", "HEAD:main", cwd=work2)
    _shutil.rmtree(work2)

    # Refresh git source to pick up removal.
    gs.refresh()

    # Re-provision the original workspace — foo should be revoked.
    result3 = provisioner.provision(rw, user)
    assert "foo" in result3.revoked, f"Expected foo in revoked, got {result3}"
    # Physical removal: the skill dir must be gone.
    assert not (ws_dir / ".agents" / "skills" / "foo").exists(), (
        "Revoked skill directory must be physically removed"
    )

    # --- Phase 4: path traversal in workspace allowed_skills (engine-side guard). ---
    rw_evil = _resolved(ws_dir, ("../evil",))
    result4 = provisioner.provision(rw_evil, user)
    assert "../evil" in result4.invalid, (
        "Path traversal in allowed_skills must be caught by Provisioner.validate_skill_name"
    )
