from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass
from claw_engine.engine.context.workspace import ResolvedWorkspace
from claw_engine.engine.identity.contracts import IdentityProvider, User
from claw_engine.engine.skills.source import (
    InvalidSkillName, SkillSource, validate_skill_name,
)

_DEFAULT_SKILLS_SUBDIR = os.path.join(".agents", "skills")
_MANIFEST_FILE = ".claw_provisioned.json"
_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class ProvisionResult:
    provisioned: tuple[str, ...]   # 本次实际写入/刷新的 skill
    denied: tuple[str, ...]        # allowed 但 can_use_skill=False
    missing: tuple[str, ...]       # 源里完全没有该 skill 目录
    invalid: tuple[str, ...]       # 名字非法（路径穿越）OR 源目录存在但无 SKILL.md（malformed）
    revoked: tuple[str, ...]       # 上次本工具 provisioned 但本次不再 effective，已从磁盘清理


class SkillProvisioner:
    """把 allowed_skills ∩ 用户授权的 skill 从 SkillSource 同步到 cwd/.agents/skills，
    并通过 manifest 跟踪「本工具管理的 skill」、撤权时清理失效项；用户自建目录从不入 manifest，永远不动。
    """

    def __init__(self, source: SkillSource, identity: IdentityProvider, *,
                 skills_subdir: str = _DEFAULT_SKILLS_SUBDIR) -> None:
        self._source = source
        self._identity = identity
        self._skills_subdir = skills_subdir

    def provision(self, workspace: ResolvedWorkspace, user: User) -> ProvisionResult:
        dest_root = os.path.join(workspace.cwd, self._skills_subdir)
        os.makedirs(dest_root, exist_ok=True)
        prev_managed = self._read_manifest(dest_root)   # 上次本工具管理的 skill 集

        provisioned: list[str] = []
        denied: list[str] = []
        missing: list[str] = []
        invalid: list[str] = []

        for skill in workspace.allowed_skills:
            try:
                validate_skill_name(skill)              # 防路径穿越：先校验名字
            except InvalidSkillName:
                invalid.append(skill)
                continue
            if not self._identity.can_use_skill(user, workspace.workspace_id, skill):
                denied.append(skill)
                continue
            if not self._source.has_skill(skill):
                # 区分：源完全没有 vs 目录在但无 SKILL.md（malformed）
                if self._source.has_dir(skill):
                    invalid.append(skill)
                else:
                    missing.append(skill)
                continue
            src = self._source.skill_dir(skill)
            dst = os.path.join(dest_root, skill)
            shutil.copytree(src, dst, dirs_exist_ok=True)
            provisioned.append(skill)

        # 撤权清理：上次本工具 provisioned 但本次不再 effective 的 skill -> 从磁盘移除
        # 关键：只清理 manifest 里记的（本工具管理的），用户自建目录绝不动
        # 双重防御：(1) _read_manifest 已过滤非法名；(2) rmtree 前再做 realpath 包含校验
        effective = set(provisioned)
        revoked: list[str] = []
        dest_root_real = os.path.realpath(dest_root)
        for prev_skill in sorted(prev_managed - effective):
            target = os.path.join(dest_root, prev_skill)
            target_real = os.path.realpath(target)
            try:
                common = os.path.commonpath([dest_root_real, target_real])
            except ValueError:                                  # 跨盘等无公共路径
                continue
            if common != dest_root_real or target_real == dest_root_real:
                continue                                        # 越界 / 指向 dest_root 本身 -> 跳过
            if os.path.isdir(target_real):
                shutil.rmtree(target_real)
                revoked.append(prev_skill)

        # 写新 manifest（只记录本次本工具实际管理的 skill）
        self._write_manifest(dest_root, effective)

        return ProvisionResult(tuple(provisioned), tuple(denied), tuple(missing),
                               tuple(invalid), tuple(revoked))

    def _manifest_path(self, dest_root: str) -> str:
        return os.path.join(dest_root, _MANIFEST_FILE)

    def _read_manifest(self, dest_root: str) -> set:
        """读取 manifest 并对每条 managed 做 validate_skill_name；非法项一律丢弃，
        避免被篡改的 manifest 让撤权清理穿越到 dest_root 之外（这是 RBAC 闸口的一部分）。"""
        try:
            with open(self._manifest_path(dest_root), encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        managed: set = set()
        for entry in data.get("managed", []):
            if not isinstance(entry, str):
                continue
            try:
                validate_skill_name(entry)
            except InvalidSkillName:
                continue                                    # 篡改/非法名 -> 忽略
            managed.add(entry)
        return managed

    def _write_manifest(self, dest_root: str, managed: set) -> None:
        with open(self._manifest_path(dest_root), "w", encoding="utf-8") as f:
            json.dump({"version": _MANIFEST_VERSION, "managed": sorted(managed)}, f)
