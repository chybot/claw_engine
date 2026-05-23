import os
from claw_engine.engine.identity.contracts import User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.context.workspace import ResolvedWorkspace
from claw_engine.engine.skills.source import LocalDirSkillSource
from claw_engine.engine.skills.provisioner import SkillProvisioner


def _source(tmp_path):
    root = tmp_path / "skills_src"
    for name in ("abtest", "dag_tracer"):     # 注意：jira-assigner 故意不放，制造 missing
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
        (root / name / "run.py").write_text("print('x')", encoding="utf-8")
    return LocalDirSkillSource(str(root))


def _resolved(cwd, allowed):
    return ResolvedWorkspace(workspace_id="ws1", cwd=str(cwd), env={}, sensitive_keys=frozenset(),
                             allowed_skills=allowed)


def _provisioner(tmp_path, denied=()):
    identity = InMemoryIdentityProvider(
        users={"ref-u1": User(user_id="u1", display_name="A", default_workspace="ws1")},
        authorized={"u1": ("ws1",)}, denied_skills={"u1": tuple(denied)},
    )
    return SkillProvisioner(_source(tmp_path), identity), identity.resolve_user("ref-u1")


def test_provisions_allowed_and_permitted_skills_to_disk(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path)
    rw = _resolved(cwd, ("abtest", "dag_tracer"))
    result = prov.provision(rw, user)
    assert set(result.provisioned) == {"abtest", "dag_tracer"}
    # 真实落盘到 .agents/skills
    assert os.path.isfile(cwd / ".agents" / "skills" / "abtest" / "SKILL.md")
    assert os.path.isfile(cwd / ".agents" / "skills" / "dag_tracer" / "run.py")


def test_denied_skill_not_provisioned(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path, denied=("dag_tracer",))
    rw = _resolved(cwd, ("abtest", "dag_tracer"))
    result = prov.provision(rw, user)
    assert result.provisioned == ("abtest",)
    assert result.denied == ("dag_tracer",)
    assert not os.path.exists(cwd / ".agents" / "skills" / "dag_tracer")   # 被 RBAC 挡，未落盘


def test_missing_skill_reported_not_provisioned(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path)
    rw = _resolved(cwd, ("abtest", "jira-assigner"))   # jira-assigner 不在 source
    result = prov.provision(rw, user)
    assert result.provisioned == ("abtest",)
    assert result.missing == ("jira-assigner",)


def test_skill_not_in_allowed_is_ignored(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path)
    rw = _resolved(cwd, ("abtest",))                   # dag_tracer 在 source 但不在 allowed
    result = prov.provision(rw, user)
    assert result.provisioned == ("abtest",)
    assert not os.path.exists(cwd / ".agents" / "skills" / "dag_tracer")   # workspace 白名单外不碰


def test_revoked_skill_is_removed_from_disk(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path)
    # 第一次：两个都 provision
    prov.provision(_resolved(cwd, ("abtest", "dag_tracer")), user)
    assert os.path.isdir(cwd / ".agents" / "skills" / "dag_tracer")
    # 第二次：把 dag_tracer 从 allowed 移走 -> 应被清理（撤权闸口生效）
    result = prov.provision(_resolved(cwd, ("abtest",)), user)
    assert "dag_tracer" in result.revoked
    assert not os.path.exists(cwd / ".agents" / "skills" / "dag_tracer")
    assert os.path.isdir(cwd / ".agents" / "skills" / "abtest")            # 仍 effective 的保留


def test_user_placed_dir_not_in_manifest_is_never_touched(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path)
    prov.provision(_resolved(cwd, ("abtest",)), user)
    # 用户自建一个目录（非本工具管理）
    user_custom = cwd / ".agents" / "skills" / "user_custom"
    user_custom.mkdir()
    (user_custom / "marker.txt").write_text("by user", encoding="utf-8")
    # 再次 provision 空 allowed_skills；abtest 应被撤、user_custom 不动
    result = prov.provision(_resolved(cwd, ()), user)
    assert "abtest" in result.revoked
    assert not os.path.exists(cwd / ".agents" / "skills" / "abtest")
    assert (user_custom / "marker.txt").read_text(encoding="utf-8") == "by user"   # 用户文件完好


def test_invalid_skill_name_rejected_no_read_no_write(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    prov, user = _provisioner(tmp_path)
    rw = _resolved(cwd, ("../evil", "/tmp/evil", "a/b", ".", "..", "abtest"))
    result = prov.provision(rw, user)
    assert result.provisioned == ("abtest",)
    assert set(result.invalid) == {"../evil", "/tmp/evil", "a/b", ".", ".."}
    # 关键：dest 下不应出现任何穿越路径产物（用 os.path 检查字面路径，不让 pathlib 解析 ../.）
    skills_dir = str(cwd / ".agents" / "skills")
    for bad in ("evil", "tmp"):
        # 防穿越产物（../evil 不得在 skills_dir 上方产生 evil；/tmp/evil 不得产生 tmp）
        assert not os.path.exists(os.path.join(skills_dir, bad))
    # 确认 skills_dir 本身只含 abtest 和 manifest
    entries = set(os.listdir(skills_dir))
    assert "abtest" in entries
    assert not entries - {"abtest", ".claw_provisioned.json"}


def test_source_dir_without_skill_md_is_invalid(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    # 在 source 加个无 SKILL.md 的目录（先确保 skills_src 存在）
    (tmp_path / "skills_src" / "bad_skill").mkdir(parents=True)
    prov, user = _provisioner(tmp_path)
    rw = _resolved(cwd, ("abtest", "bad_skill"))
    result = prov.provision(rw, user)
    assert result.provisioned == ("abtest",)
    assert result.invalid == ("bad_skill",)             # 目录在但无 SKILL.md -> invalid（非 missing）
    assert not os.path.exists(cwd / ".agents" / "skills" / "bad_skill")
