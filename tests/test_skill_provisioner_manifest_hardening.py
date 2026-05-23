"""Manifest-tampering hardening: a forged .claw_provisioned.json must never
let SkillProvisioner.provision() delete anything outside the skills subdir.

Defense in depth: (1) _read_manifest drops entries that fail validate_skill_name,
(2) rmtree path is realpath-contained under dest_root.
"""

from __future__ import annotations
import json
from pathlib import Path

from claw_engine.engine.context.workspace import ResolvedWorkspace
from claw_engine.engine.identity.contracts import User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.skills.provisioner import SkillProvisioner
from claw_engine.engine.skills.source import LocalDirSkillSource


def _setup(tmp_path: Path):
    """Build a source (with one valid skill) and a workspace cwd containing a
    .agents/skills dir plus a SIBLING ".agents/victim/important.txt" that
    must survive any provision()."""
    src = tmp_path / "src"
    (src / "abtest").mkdir(parents=True)
    (src / "abtest" / "SKILL.md").write_text("# abtest", encoding="utf-8")

    cwd = tmp_path / "ws"
    cwd.mkdir()
    victim = cwd / ".agents" / "victim"
    victim.mkdir(parents=True)
    (victim / "important.txt").write_text("IMPORTANT", encoding="utf-8")

    skills_dir = cwd / ".agents" / "skills"
    skills_dir.mkdir()

    identity = InMemoryIdentityProvider(
        users={"r": User(user_id="u", display_name="A", default_workspace="ws1")},
        authorized={"u": ("ws1",)},
    )
    prov = SkillProvisioner(LocalDirSkillSource(str(src)), identity)
    user = identity.resolve_user("r")
    rw = ResolvedWorkspace(
        workspace_id="ws1", cwd=str(cwd), env={}, sensitive_keys=frozenset(),
        allowed_skills=("abtest",),
    )
    return prov, user, rw, cwd, skills_dir, victim


def _write_manifest(skills_dir: Path, managed: list) -> None:
    (skills_dir / ".claw_provisioned.json").write_text(
        json.dumps({"version": 1, "managed": managed}), encoding="utf-8"
    )


def test_forged_manifest_with_path_traversal_does_not_escape_dest(tmp_path):
    prov, user, rw, cwd, skills_dir, victim = _setup(tmp_path)
    _write_manifest(skills_dir, ["../victim"])           # 试图穿越到 .agents/victim
    result = prov.provision(rw, user)

    assert victim.exists()                                # 目录仍在
    assert (victim / "important.txt").exists()            # 文件未被删
    assert (victim / "important.txt").read_text(encoding="utf-8") == "IMPORTANT"
    assert "../victim" not in result.revoked              # 非法名不计入 revoked


def test_forged_manifest_with_absolute_path_is_ignored(tmp_path):
    prov, user, rw, cwd, skills_dir, victim = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUT", encoding="utf-8")
    _write_manifest(skills_dir, [str(outside)])           # 绝对路径

    result = prov.provision(rw, user)

    assert outside.exists() and (outside / "secret.txt").exists()  # 外部完好
    assert str(outside) not in result.revoked


def test_forged_manifest_with_slash_segment_is_ignored(tmp_path):
    prov, user, rw, cwd, skills_dir, victim = _setup(tmp_path)
    _write_manifest(skills_dir, ["a/b"])                  # 子路径形式
    result = prov.provision(rw, user)
    # 没有 cwd/.agents/skills/a 子目录被创建/删除
    assert not (skills_dir / "a").exists()
    assert "a/b" not in result.revoked


def test_forged_manifest_mixed_with_valid_entries_only_revokes_valid(tmp_path):
    prov, user, rw, cwd, skills_dir, victim = _setup(tmp_path)
    # 准备一个真存在的 managed skill: dag_tracer
    (skills_dir / "dag_tracer").mkdir()
    (skills_dir / "dag_tracer" / "SKILL.md").write_text("# d", encoding="utf-8")
    _write_manifest(skills_dir, ["dag_tracer", "../victim", "/etc/passwd", ".", ".."])

    result = prov.provision(rw, user)

    # 合法的 dag_tracer 被撤；非法项一律被忽略，不计入 revoked
    assert result.revoked == ("dag_tracer",)
    assert not (skills_dir / "dag_tracer").exists()
    # 外部目录与文件完好
    assert victim.exists() and (victim / "important.txt").exists()


def test_existing_revocation_and_user_dirs_still_work_after_hardening(tmp_path):
    """老的撤权 / 用户自建目录不动 行为不能因加固而回归。"""
    prov, user, rw, cwd, skills_dir, victim = _setup(tmp_path)
    # 首轮：provision abtest（写入合法 manifest）
    prov.provision(rw, user)
    # 用户自建目录
    user_custom = skills_dir / "user_custom"
    user_custom.mkdir()
    (user_custom / "marker.txt").write_text("user", encoding="utf-8")
    # 再次 provision 空 allowed -> abtest 应被撤、user_custom 不动
    rw_empty = ResolvedWorkspace(
        workspace_id="ws1", cwd=str(cwd), env={}, sensitive_keys=frozenset(),
        allowed_skills=(),
    )
    result = prov.provision(rw_empty, user)
    assert result.revoked == ("abtest",)
    assert not (skills_dir / "abtest").exists()
    assert (user_custom / "marker.txt").read_text(encoding="utf-8") == "user"
    # 外部依旧
    assert (victim / "important.txt").exists()
