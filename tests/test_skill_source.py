# tests/test_skill_source.py
import pytest
from claw_engine.engine.skills.source import LocalDirSkillSource, SkillNotFound

def _make_source(tmp_path):
    (tmp_path / "abtest").mkdir()
    (tmp_path / "abtest" / "SKILL.md").write_text("# abtest", encoding="utf-8")
    (tmp_path / "dag_tracer").mkdir()
    (tmp_path / "dag_tracer" / "SKILL.md").write_text("# dag", encoding="utf-8")
    (tmp_path / "bad_skill").mkdir()              # 目录在但无 SKILL.md（malformed）
    return LocalDirSkillSource(str(tmp_path))

def test_has_skill_true_when_dir_and_skill_md(tmp_path):
    assert _make_source(tmp_path).has_skill("abtest") is True

def test_has_skill_false_when_missing_dir(tmp_path):
    assert _make_source(tmp_path).has_skill("ghost") is False

def test_has_skill_false_when_missing_skill_md(tmp_path):
    assert _make_source(tmp_path).has_skill("bad_skill") is False   # 目录在但无 SKILL.md

def test_has_dir_distinguishes_malformed_from_missing(tmp_path):
    src = _make_source(tmp_path)
    assert src.has_dir("bad_skill") is True         # 目录存在（不验 SKILL.md）
    assert src.has_dir("ghost") is False            # 完全没有

def test_skill_dir_returns_path(tmp_path):
    src = _make_source(tmp_path)
    assert src.skill_dir("abtest").endswith("abtest")

def test_skill_dir_raises_for_missing_or_malformed(tmp_path):
    src = _make_source(tmp_path)
    with pytest.raises(SkillNotFound):
        src.skill_dir("ghost")
    with pytest.raises(SkillNotFound):
        src.skill_dir("bad_skill")                  # 目录在但无 SKILL.md

def test_invalid_skill_names_rejected_no_read(tmp_path):
    from claw_engine.engine.skills.source import InvalidSkillName
    src = _make_source(tmp_path)
    for bad in ("../evil", "/tmp/evil", "a/b", ".", "..", "a\\b"):
        assert src.has_skill(bad) is False          # has_skill 拒绝
        assert src.has_dir(bad) is False            # has_dir 也拒绝
        with pytest.raises(InvalidSkillName):
            src.skill_dir(bad)                      # skill_dir 抛 InvalidSkillName

def test_validate_skill_name_accepts_good_names():
    from claw_engine.engine.skills.source import validate_skill_name
    for ok in ("abtest", "ws.1_a-b", "dag_tracer"):
        validate_skill_name(ok)                     # 不抛即可
