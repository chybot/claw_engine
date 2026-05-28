"""Shared SkillSource behaviour contract.

Both LocalDirSkillSource and GitSkillSource (after refresh) must satisfy
every assertion in this module.  Parametrisation lives in
test_skillsource_contract.py.

Factory type
------------
Each implementation provides a ``MakeSkillSource`` callable that accepts
``(tmp_path: Path)`` and returns a ``SkillSource`` ready for reading
(GitSkillSource has already been refreshed before the assertions run).

The fixture builds a repo / dir with:
  - ``foo/SKILL.md``   (valid skill)
  - ``bar/``           (dir only — no SKILL.md; 'invalid' / has_dir but not has_skill)
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from claw_engine.engine.skills.source import (
    InvalidSkillName,
    SkillNotFound,
    SkillSource,
)

# Factory: (tmp_path) → SkillSource ready for reading.
MakeSkillSource = Callable[[Path], SkillSource]


# ── Contract assertions ───────────────────────────────────────────────────────


def assert_has_skill_true_when_skill_md_present(make: MakeSkillSource, tmp_path: Path) -> None:
    source = make(tmp_path)
    assert source.has_skill("foo") is True


def assert_has_skill_false_for_missing(make: MakeSkillSource, tmp_path: Path) -> None:
    source = make(tmp_path)
    assert source.has_skill("missing") is False


def assert_has_dir_true_false(make: MakeSkillSource, tmp_path: Path) -> None:
    source = make(tmp_path)
    assert source.has_dir("foo") is True
    assert source.has_dir("missing") is False


def assert_skill_dir_returns_path(make: MakeSkillSource, tmp_path: Path) -> None:
    import os
    source = make(tmp_path)
    d = source.skill_dir("foo")
    assert os.path.isdir(d)
    assert os.path.isfile(os.path.join(d, "SKILL.md"))


def assert_skill_dir_raises_not_found(make: MakeSkillSource, tmp_path: Path) -> None:
    source = make(tmp_path)
    try:
        source.skill_dir("missing")
        raise AssertionError("Expected SkillNotFound but nothing was raised")
    except SkillNotFound:
        pass


def assert_invalid_name_raises_invalid_skill_name(make: MakeSkillSource, tmp_path: Path) -> None:
    """Invalid names should raise InvalidSkillName (or return False for has_*)."""
    source = make(tmp_path)
    try:
        source.skill_dir("../evil")
        raise AssertionError("Expected InvalidSkillName but nothing was raised")
    except InvalidSkillName:
        pass
    # has_skill with invalid name returns False (does not raise)
    assert source.has_skill("../evil") is False
    assert source.has_dir("../evil") is False


def assert_dir_without_skill_md_has_dir_true_has_skill_false(
    make: MakeSkillSource, tmp_path: Path
) -> None:
    """A dir without SKILL.md: has_dir=True, has_skill=False (the 'invalid' bucket)."""
    source = make(tmp_path)
    assert source.has_dir("bar") is True
    assert source.has_skill("bar") is False
