"""GitSkillSource — materialise a remote git repo into a local cache dir.

Lifecycle (strict 3-phase separation):
  1. Construction  — argument validation only; no network, no disk write.
  2. refresh()     — git subprocess, write to cache_dir.
  3. Read          — has_skill / has_dir / skill_dir delegate to LocalDirSkillSource;
                     NEVER invoke subprocess.

System requirement: ``git`` must be on PATH.  No Python git library is used.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from claw_engine.engine.skills.source import (
    LocalDirSkillSource,
    SkillNotFound,
    validate_skill_name,
)
from claw_engine.adapters.skills.git.errors import GitSkillSourceError

# Max character length for stderr snippets stored on the exception.
_STDERR_TRIM = 1000
# Maximum length for a single allowed_skill_path entry.
_MAX_PATH_LEN = 256


# ── Path validation ──────────────────────────────────────────────────────────


def _validate_allowed_path(p: str) -> None:
    """Security gate: every entry in allowed_skill_paths must pass this check.

    Raises GitSkillSourceError(stage='validate') on any rejection.
    Validation runs BEFORE any subprocess call.

    Reject conditions (in order):
    - leading or trailing whitespace
    - length > _MAX_PATH_LEN
    - starts with '/' or '\\'  (absolute / UNC paths)
    - os.path.isabs(p)          (platform-aware absolute detection)
    - '\\' in p                 (Windows path / escape attempt)
    - any segment is empty      (e.g. 'foo//bar' → '' segment)
    - any segment is '..'       (traversal)
    """
    if p != p.strip():
        raise GitSkillSourceError(
            f"allowed_skill_paths entry has leading/trailing whitespace: {p!r}",
            stage="validate",
        )
    if len(p) > _MAX_PATH_LEN:
        raise GitSkillSourceError(
            f"allowed_skill_paths entry too long ({len(p)} > {_MAX_PATH_LEN}): {p!r}",
            stage="validate",
        )
    if p.startswith("/") or p.startswith("\\"):
        raise GitSkillSourceError(
            f"allowed_skill_paths entry is absolute: {p!r}",
            stage="validate",
        )
    if os.path.isabs(p):
        raise GitSkillSourceError(
            f"allowed_skill_paths entry is absolute: {p!r}",
            stage="validate",
        )
    if "\\" in p:
        raise GitSkillSourceError(
            f"allowed_skill_paths entry contains backslash: {p!r}",
            stage="validate",
        )
    segments = p.split("/")
    for seg in segments:
        if seg == "":
            raise GitSkillSourceError(
                f"allowed_skill_paths entry has empty segment (double slash?): {p!r}",
                stage="validate",
            )
        if seg == "..":
            raise GitSkillSourceError(
                f"allowed_skill_paths entry contains '..' traversal: {p!r}",
                stage="validate",
            )
    # Validate the basename as a skill name (engine contract).
    basename = segments[-1]
    try:
        validate_skill_name(basename)
    except Exception as exc:
        raise GitSkillSourceError(
            f"allowed_skill_paths basename is not a valid skill name: {basename!r}",
            stage="validate",
        ) from exc


# ── git subprocess helper ────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Run a git command; raise GitSkillSourceError on non-zero exit or missing git."""
    # The stage label is the first argument (clone, fetch, checkout, sparse-checkout, …).
    stage = args[0] if args else "invoke"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GitSkillSourceError(
            "git executable not found on PATH",
            stage="invoke",
        ) from exc
    except OSError as exc:
        raise GitSkillSourceError(
            f"OS error invoking git: {exc}",
            stage="invoke",
        ) from exc

    if result.returncode != 0:
        stderr_trimmed = (result.stderr or "")[:_STDERR_TRIM]
        raise GitSkillSourceError(
            f"git {stage} failed (exit {result.returncode}): {stderr_trimmed}",
            stage=stage,
            returncode=result.returncode,
            stderr=stderr_trimmed,
        )
    return result


# ── RefreshResult ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefreshResult:
    """Return value of GitSkillSource.refresh().

    Attributes:
        ref: the commit SHA that is now checked out in cache_dir.
        materialized_paths: tuple of skill *names* (basenames) now available.
    """

    ref: str
    materialized_paths: tuple[str, ...]


# ── GitSkillSource ────────────────────────────────────────────────────────────


class GitSkillSource:
    """SkillSource backed by a remote git repository.

    Construction is hermetic (no IO).  Call refresh() to materialise the
    repository into cache_dir, then use has_skill / has_dir / skill_dir.

    Args:
        repo_url: any URL that ``git clone`` accepts (https://, git@, file://).
        ref: branch name, tag, or commit SHA to check out.
        cache_dir: local directory for the working tree (created on refresh()).
        allowed_skill_paths: if given, sparse-checkout exactly these paths
            (relative to the repo root).  When None (default), clones the full
            tree and derives skill names from a one-level scan of cache_dir.
        eager: when True, calls refresh() during __init__.

    Thread safety: refresh() is NOT thread-safe.  Concurrent callers sharing
    the same cache_dir must serialise externally.
    """

    def __init__(
        self,
        repo_url: str,
        ref: str,
        cache_dir: str | Path,
        *,
        allowed_skill_paths: Sequence[str] | None = None,
        eager: bool = False,
    ) -> None:
        if not repo_url:
            raise ValueError("repo_url must be a non-empty string")
        if not ref:
            raise ValueError("ref must be a non-empty string")

        self._repo_url: str = repo_url
        self._ref: str = ref
        self._cache_dir: Path = Path(cache_dir)
        self._allowed_skill_paths: tuple[str, ...] | None = (
            tuple(allowed_skill_paths) if allowed_skill_paths is not None else None
        )
        # Set during refresh(); None means "not yet refreshed".
        self._local: LocalDirSkillSource | None = None

        if eager:
            self.refresh()

    # ── Refresh ──────────────────────────────────────────────────────────────

    def refresh(self, *, ref: str | None = None) -> RefreshResult:
        """Materialise (or update) the git checkout in cache_dir.

        Args:
            ref: optional ref override.  When supplied, updates self._ref.

        Returns:
            RefreshResult with the resolved commit SHA and materialized names.

        Raises:
            GitSkillSourceError on any git failure or path validation error.
        """
        # --- optional ref override ---
        if ref is not None:
            if not ref:
                raise ValueError("ref override must be a non-empty string")
            self._ref = ref

        # --- validate allowed_skill_paths BEFORE any subprocess ---
        if self._allowed_skill_paths is not None:
            for p in self._allowed_skill_paths:
                _validate_allowed_path(p)

        # --- ensure cache_dir exists ---
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GitSkillSourceError(
                f"Failed to create cache_dir {self._cache_dir}: {exc}",
                stage="mkdir",
            ) from exc

        # --- clone if not already a git repo ---
        git_dir = self._cache_dir / ".git"
        if not git_dir.exists():
            _git(
                "clone",
                "--depth=1",
                "--filter=blob:none",
                "--no-checkout",
                self._repo_url,
                str(self._cache_dir),
                cwd=self._cache_dir.parent,
            )

        # --- sparse-checkout setup ---
        if self._allowed_skill_paths is not None:
            _git("sparse-checkout", "init", "--cone", cwd=self._cache_dir)
            _git(
                "sparse-checkout",
                "set",
                *self._allowed_skill_paths,
                cwd=self._cache_dir,
            )
        else:
            # Full tree — disable sparse-checkout if it was previously enabled.
            try:
                _git("sparse-checkout", "disable", cwd=self._cache_dir)
            except GitSkillSourceError:
                # older git versions may not support sparse-checkout disable;
                # ignore if it fails (full checkout proceeds normally).
                pass

        # --- fetch then checkout via FETCH_HEAD (works for branches, tags, SHAs) ---
        # We use `--detach FETCH_HEAD` rather than `checkout <ref>` because after a
        # depth-1 fetch of a branch, git does not create a local tracking ref for
        # branches that weren't cloned.  FETCH_HEAD always points to what was just
        # fetched, so this approach works uniformly for branches, tags, and commit SHAs.
        _git("fetch", "--depth=1", "origin", self._ref, cwd=self._cache_dir)
        _git("checkout", "--detach", "FETCH_HEAD", cwd=self._cache_dir)

        # --- resolve the commit SHA ---
        resolved_sha = _git(
            "rev-parse", "HEAD", cwd=self._cache_dir
        ).stdout.strip()

        # --- build LocalDirSkillSource and derive skill names ---
        if self._allowed_skill_paths is not None:
            # Skill name = basename of each allowed path.
            skill_names = tuple(
                os.path.basename(p) for p in self._allowed_skill_paths
            )
            # Check if any path is nested (contains a '/').  When paths are
            # nested the skill directory lives at cache_dir/<full_path> but
            # LocalDirSkillSource expects cache_dir/<basename>/SKILL.md.
            # Build a flat overlay using symlinks so basenames resolve correctly.
            any_nested = any("/" in p for p in self._allowed_skill_paths)
            if any_nested:
                flat_dir = self._cache_dir / ".claw_skills_flat"
                flat_dir.mkdir(exist_ok=True)
                for p in self._allowed_skill_paths:
                    basename = os.path.basename(p)
                    link_path = flat_dir / basename
                    # Remove stale symlink / dir from previous refresh.
                    if link_path.is_symlink() or link_path.exists():
                        if link_path.is_symlink():
                            link_path.unlink()
                        else:
                            import shutil as _shutil
                            _shutil.rmtree(link_path)
                    target = self._cache_dir / p
                    if target.exists():
                        link_path.symlink_to(target)
                self._local = LocalDirSkillSource(str(flat_dir))
            else:
                self._local = LocalDirSkillSource(str(self._cache_dir))
        else:
            # One-level scan of cache_dir (exclude .git and dotfiles).
            skill_names = tuple(
                entry.name
                for entry in sorted(self._cache_dir.iterdir())
                if entry.is_dir() and not entry.name.startswith(".")
            )
            self._local = LocalDirSkillSource(str(self._cache_dir))
        return RefreshResult(ref=resolved_sha, materialized_paths=skill_names)

    # ── Read methods (hermetic — never invoke subprocess) ────────────────────

    def has_skill(self, name: str) -> bool:
        """Return True iff name exists in cache and has SKILL.md.

        Returns False when not yet refreshed.  Never invokes git.
        """
        if self._local is None:
            return False
        return self._local.has_skill(name)

    def has_dir(self, name: str) -> bool:
        """Return True iff name exists as a directory in cache.

        Returns False when not yet refreshed.  Never invokes git.
        """
        if self._local is None:
            return False
        return self._local.has_dir(name)

    def skill_dir(self, name: str) -> str:
        """Return the path to the skill directory.

        Raises:
            InvalidSkillName: if name fails validate_skill_name.
            SkillNotFound: if the skill is not present (including pre-refresh).

        Never invokes git.
        """
        if self._local is None:
            # Validate the name (re-raise InvalidSkillName if bad).
            validate_skill_name(name)
            raise SkillNotFound(name)
        return self._local.skill_dir(name)
