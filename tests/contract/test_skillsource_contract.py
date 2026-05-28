"""Parametrised SkillSource contract tests.

Runs every assertion in skillsource_contract.py against:
  - LocalDirSkillSource  (engine built-in)
  - GitSkillSource       (adapter, refreshed against a local bare repo)

The module-level skip gates on system git availability.
"""
from __future__ import annotations

import shutil

import pytest

# Gate on git availability.
if shutil.which("git") is None:
    pytest.skip("git not on PATH", allow_module_level=True)

from pathlib import Path

import subprocess

from claw_engine.engine.skills.source import LocalDirSkillSource
from claw_engine.adapters.skills.git import GitSkillSource
from tests.contract import skillsource_contract as sc


# ── Factories ────────────────────────────────────────────────────────────────


def _make_local_dir(tmp_path: Path) -> LocalDirSkillSource:
    """Create a LocalDirSkillSource with foo (valid) and bar (no SKILL.md)."""
    root = tmp_path / "skills_local"
    root.mkdir()
    (root / "foo").mkdir()
    (root / "foo" / "SKILL.md").write_text("# foo", encoding="utf-8")
    (root / "bar").mkdir()  # no SKILL.md — 'invalid' bucket
    return LocalDirSkillSource(str(root))


def _make_git_source(tmp_path: Path) -> GitSkillSource:
    """Create a GitSkillSource, refresh it against a bare repo.

    The repo contains:
      - foo/SKILL.md  (valid skill)
      - bar/run.py    (dir without SKILL.md — 'invalid' bucket)
    """
    import shutil as _shutil

    repo_base = tmp_path / "git_mixed"
    repo_base.mkdir(parents=True, exist_ok=True)
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

    (work / "foo").mkdir()
    (work / "foo" / "SKILL.md").write_text("# foo", encoding="utf-8")
    (work / "bar").mkdir()
    (work / "bar" / "run.py").write_text("# no SKILL.md", encoding="utf-8")

    _r("git", "add", ".", cwd=work)
    _r("git", "commit", "-m", "init", cwd=work)
    _r("git", "push", "origin", "HEAD:main", cwd=work)
    _shutil.rmtree(work)

    cache = tmp_path / "git_cache"
    gs = GitSkillSource(f"file://{bare}", "main", cache)
    gs.refresh()
    return gs


# ── Parametrisation ──────────────────────────────────────────────────────────


SOURCES = [
    ("local_dir", _make_local_dir),
    ("git", _make_git_source),
]


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_has_skill_true_when_skill_md_present(name, make, tmp_path):
    sc.assert_has_skill_true_when_skill_md_present(make, tmp_path)


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_has_skill_false_for_missing(name, make, tmp_path):
    sc.assert_has_skill_false_for_missing(make, tmp_path)


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_has_dir_true_false(name, make, tmp_path):
    sc.assert_has_dir_true_false(make, tmp_path)


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_skill_dir_returns_path(name, make, tmp_path):
    sc.assert_skill_dir_returns_path(make, tmp_path)


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_skill_dir_raises_not_found(name, make, tmp_path):
    sc.assert_skill_dir_raises_not_found(make, tmp_path)


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_invalid_name_raises_invalid_skill_name(name, make, tmp_path):
    sc.assert_invalid_name_raises_invalid_skill_name(make, tmp_path)


@pytest.mark.parametrize("name,make", SOURCES, ids=[n for n, _ in SOURCES])
def test_dir_without_skill_md_has_dir_true_has_skill_false(name, make, tmp_path):
    sc.assert_dir_without_skill_md_has_dir_true_has_skill_false(make, tmp_path)
