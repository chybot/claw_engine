"""Fixture builders for SkillSource contract tests.

Provides lightweight local bare-repo builders using system git.
``file://`` URLs are used so the adapter exercises the same code path
as a real remote (avoids git's local-repo hardlink optimisation).

No testcontainers or network access required.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping


def _run(*args: str, cwd: Path) -> None:
    """Run a git command; raise on failure."""
    result = subprocess.run(
        list(args),
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {args!r} failed (exit {result.returncode}):\n{result.stderr}"
        )


def make_bare_repo_with_skills(
    base: Path,
    *,
    skills: Mapping[str, str],
    layout: str = "flat",
) -> str:
    """Build a local bare repo with skills committed on ``main``.

    Args:
        base: parent directory for the bare repo and working tree.
        skills: mapping of skill_name → SKILL.md content.
        layout: ``"flat"`` places skills at the repo root (``<name>/SKILL.md``);
                ``"nested"`` places them under ``skills/<name>/SKILL.md``.

    Returns:
        A ``file://`` URL string pointing to the bare repo.
    """
    base.mkdir(parents=True, exist_ok=True)
    bare = base / "repo.git"
    work = base / "work"

    # 1. Init bare repo.
    _run("git", "init", "--bare", str(bare), cwd=base)

    # 2. Clone into work dir.
    _run("git", "clone", f"file://{bare}", str(work), cwd=base)

    # Configure identity in the work repo so commits succeed.
    _run("git", "config", "user.email", "test@test.com", cwd=work)
    _run("git", "config", "user.name", "Test", cwd=work)

    # 3. Write skill files.
    for name, content in skills.items():
        if layout == "flat":
            skill_dir = work / name
        else:
            skill_dir = work / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    # 4. Commit and push (create initial commit even if skills is empty).
    _run("git", "add", ".", cwd=work)
    # Use --allow-empty so the fixture works even with no skills.
    _run(
        "git",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
        cwd=work,
    )
    # Push to establish 'main' on the bare repo.
    _run("git", "push", "origin", "HEAD:main", cwd=work)

    # 5. Clean up work tree (bare repo is all we need).
    import shutil
    shutil.rmtree(work)

    return f"file://{bare}"


def make_bare_repo_missing_skill_md(
    base: Path,
    *,
    skill_name: str = "broken_skill",
) -> str:
    """Bare repo with a skill directory that has NO SKILL.md.

    Used to test the 'invalid' bucket (dir present, SKILL.md missing).
    """
    base.mkdir(parents=True, exist_ok=True)
    bare = base / "repo.git"
    work = base / "work"

    _run("git", "init", "--bare", str(bare), cwd=base)
    _run("git", "clone", f"file://{bare}", str(work), cwd=base)
    _run("git", "config", "user.email", "test@test.com", cwd=work)
    _run("git", "config", "user.name", "Test", cwd=work)

    # Create the skill dir but do NOT write SKILL.md.
    (work / skill_name).mkdir(parents=True, exist_ok=True)
    # Write a placeholder so git doesn't ignore the empty dir.
    (work / skill_name / "run.py").write_text("# placeholder", encoding="utf-8")

    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "-m", "initial", cwd=work)
    _run("git", "push", "origin", "HEAD:main", cwd=work)

    import shutil
    shutil.rmtree(work)

    return f"file://{bare}"


def make_bare_repo_empty(base: Path) -> str:
    """Bare repo with no skills committed (empty tree via empty commit)."""
    return make_bare_repo_with_skills(base, skills={})


def make_bare_repo_with_two_branches(
    base: Path,
    *,
    main_skills: Mapping[str, str],
    branch2_skills: Mapping[str, str],
    branch2_name: str = "branch2",
) -> str:
    """Bare repo with ``main`` + a second branch.

    Used to test ref-switching in refresh().
    """
    base.mkdir(parents=True, exist_ok=True)
    bare = base / "repo.git"
    work = base / "work"

    _run("git", "init", "--bare", str(bare), cwd=base)
    _run("git", "clone", f"file://{bare}", str(work), cwd=base)
    _run("git", "config", "user.email", "test@test.com", cwd=work)
    _run("git", "config", "user.name", "Test", cwd=work)

    # Commit main skills.
    for name, content in main_skills.items():
        skill_dir = work / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "--allow-empty", "-m", "main commit", cwd=work)
    _run("git", "push", "origin", "HEAD:main", cwd=work)

    # Create branch2 from main and commit branch2 skills.
    _run("git", "checkout", "-b", branch2_name, cwd=work)

    for name, content in branch2_skills.items():
        skill_dir = work / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "--allow-empty", "-m", f"{branch2_name} commit", cwd=work)
    _run("git", "push", "origin", f"HEAD:{branch2_name}", cwd=work)

    import shutil
    shutil.rmtree(work)

    return f"file://{bare}"
