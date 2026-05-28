"""GitSkillSource adapter — materialises skills from a remote git repository.

Requires system ``git`` on PATH; no Python git library is needed.
Install with: ``pip install -e .[skills-git]``  (no extra Python packages,
but system git must be available).
"""
from claw_engine.adapters.skills.git.errors import GitSkillSourceError
from claw_engine.adapters.skills.git.source import GitSkillSource, RefreshResult

__all__ = ["GitSkillSource", "GitSkillSourceError", "RefreshResult"]
