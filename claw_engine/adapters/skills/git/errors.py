"""Adapter-local exception for GitSkillSource failures.

Never impersonates engine exceptions (InvalidSkillName, SkillNotFound).
Callers catch this as an adapter-level failure and decide policy.
"""
from __future__ import annotations


class GitSkillSourceError(Exception):
    """Raised when a git operation or validation step fails.

    Attributes:
        stage: which phase failed ('validate', 'mkdir', 'clone',
               'sparse-checkout', 'fetch', 'checkout', 'invoke').
        returncode: git process exit code (0 when not from a subprocess).
        stderr: trimmed stderr from the git process (empty string when not
                from a subprocess).
    """

    def __init__(
        self,
        msg: str,
        *,
        stage: str,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        super().__init__(msg)
        self.stage = stage
        self.returncode = returncode
        self.stderr = stderr

    def __repr__(self) -> str:
        return (
            f"GitSkillSourceError({self.args[0]!r}, "
            f"stage={self.stage!r}, returncode={self.returncode!r})"
        )
