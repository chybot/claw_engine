from __future__ import annotations
import os
import re
from typing import Protocol, runtime_checkable

_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


class InvalidSkillName(ValueError):
    """skill 名非法（路径穿越或字符不在白名单）。"""
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def validate_skill_name(name: str) -> None:
    """只允许 [A-Za-z0-9_.-]+，且禁止 '.'/'..'，防路径穿越（../x、a/b、绝对路径等一律拒绝）。"""
    if name in (".", "..") or _SKILL_NAME_RE.fullmatch(name) is None:
        raise InvalidSkillName(name)


class SkillNotFound(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@runtime_checkable
class SkillSource(Protocol):
    def has_skill(self, name: str) -> bool: ...    # 合法名 + 目录存在 + SKILL.md 存在
    def has_dir(self, name: str) -> bool: ...      # 仅目录存在（用于区分 missing vs malformed）
    def skill_dir(self, name: str) -> str: ...     # 非法名抛 InvalidSkillName；不存在/无 SKILL.md 抛 SkillNotFound


class LocalDirSkillSource:
    """V1：本地目录的 skill 包（<root>/<name>/SKILL.md ...）。真实 git 源在 adapters。"""

    def __init__(self, root: str) -> None:
        self._root = root

    def has_skill(self, name: str) -> bool:
        try:
            validate_skill_name(name)
        except InvalidSkillName:
            return False                              # 非法名 -> False（不读 source）
        path = os.path.join(self._root, name)
        return os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md"))

    def has_dir(self, name: str) -> bool:
        try:
            validate_skill_name(name)
        except InvalidSkillName:
            return False
        return os.path.isdir(os.path.join(self._root, name))

    def skill_dir(self, name: str) -> str:
        validate_skill_name(name)                     # 非法名 -> 抛 InvalidSkillName
        path = os.path.join(self._root, name)
        if not (os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md"))):
            raise SkillNotFound(name)
        return path
