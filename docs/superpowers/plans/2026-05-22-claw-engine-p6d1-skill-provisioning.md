# claw_engine P6d-1 — Skill Provisioning + can_use_skill RBAC 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 给 IdentityProvider 加 `can_use_skill`（default-allow），并实现 `SkillProvisioner`：把「workspace.allowed_skills ∩ 用户被允许的 skill」从 `SkillSource` 真实同步到 `cwd/.agents/skills/`，供 codex/claude 原生读取。RBAC 强制点 = provisioning 时过滤。

**Architecture:** `engine/skills/`（`SkillSource` Protocol + `LocalDirSkillSource` + `SkillProvisioner` + `ProvisionResult`，业务/CLI 无关）。`can_use_skill` 加进 P6b `IdentityProvider`（加法式）。dest 子目录 `.agents/skills` 是可配默认参数（不写死 CLI 知识）。真实 skill repo 同步源（git）在 adapters 实现 `SkillSource`。

**Tech Stack:** Python 3.11+，标准库 `os`/`shutil`，pytest，ruff。全 hermetic（tmp 目录做 SkillSource 与 cwd）。

**前置参考：** P5 `ResolvedWorkspace.allowed_skills`/`cwd`、P6b `IdentityProvider`/`User`、algo-bot `skill_sync_service`（distribute→.agents/skills 思路）。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策（P6d-1 相关）
1. **拆分**：P6d-1 = skill provisioning + can_use_skill；P6d-2 = run_workflow tool bridge（独立 plan，后续）。
2. **can_use_skill default-allow**：有效 skill = `allowed_skills ∩ {s: can_use_skill(user, ws, s)}`；`can_use_skill` 默认 True（workspace 白名单之上的额外限制层）。**RBAC 强制点 = provisioning 时过滤**（CLI 原生跑 .agents/skills，引擎不拦调用，只控「写不写进去」）。
3. **真实 provisioning**：`SkillSource`(本地目录的 skill 包) → 把允许+授权的 skill 复制进 `cwd/.agents/skills/<skill>/`。hermetic 用 tmp。
4. （P6d-2 预定）WorkflowToolBridge + 权限，transport 缓后。

### 边界（P6d-1 不做）
- 不做 run_workflow tool bridge（P6d-2）。
- 不做 skill 同步的真实 git 源 / 定时同步（adapters/部署；P6d-1 用 LocalDirSkillSource）。
- **不强绑「何时 provision」到主链路**：P6d-1 交付 `SkillProvisioner` 组件 + RBAC 过滤，hermetic e2e 证明；接到 live flow（每会话/启动时 provision）属部署/setup 步骤（参考 algo-bot 周期性 skill_sync），后续。
- `can_use_skill` 的 workspace 维度：签名带 `workspace_id`，V1 InMemory 按 user_id 维护 denied 集（ws 暂忽略，留接口给 adapter 做 per-workspace）。

### provisioning 流
```
provision(resolved_workspace, user):
  dest_root = cwd / skills_subdir(默认 ".agents/skills")
  for skill in resolved_workspace.allowed_skills:
     if not identity.can_use_skill(user, ws_id, skill): denied += skill; continue
     if not source.has_skill(skill):                     missing += skill; continue
     copytree(source.skill_dir(skill) -> dest_root/skill, overwrite)
     provisioned += skill
  return ProvisionResult(provisioned, denied, missing)
```

---

### Task 1: IdentityProvider.can_use_skill（default-allow）

**Files:**
- Modify: `claw_engine/engine/identity/contracts.py`（Protocol 加 can_use_skill）
- Modify: `claw_engine/engine/identity/memory.py`（denied_skills + can_use_skill）
- Test: `tests/test_can_use_skill.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_can_use_skill.py
from claw_engine.engine.identity.contracts import User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider

def _provider():
    users = {"ref-u1": User(user_id="u1", display_name="Alice", default_workspace="ws1")}
    return InMemoryIdentityProvider(
        users=users, authorized={"u1": ("ws1",)},
        denied_skills={"u1": ("dangerous-skill",)},
    )

def test_can_use_skill_default_allow():
    p = _provider()
    u = p.resolve_user("ref-u1")
    assert p.can_use_skill(u, "ws1", "abtest") is True          # 未 deny -> 允许（default-allow）

def test_can_use_skill_denied():
    p = _provider()
    u = p.resolve_user("ref-u1")
    assert p.can_use_skill(u, "ws1", "dangerous-skill") is False  # 显式 deny

def test_can_use_skill_user_without_denies_allows_all():
    p = InMemoryIdentityProvider(
        users={"ref-u2": User(user_id="u2", display_name="Bob")},
        authorized={"u2": ("ws1",)},
    )
    u = p.resolve_user("ref-u2")
    assert p.can_use_skill(u, "ws1", "anything") is True
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_can_use_skill.py -q`
Expected: FAIL（InMemoryIdentityProvider 无 denied_skills/can_use_skill）

- [ ] **Step 3: 改实现**

`identity/contracts.py` 的 `IdentityProvider` Protocol 末尾加：
```python
    def can_use_skill(self, user: User, workspace_id: str, skill: str) -> bool: ...
```

`identity/memory.py` 的 `InMemoryIdentityProvider`：
- `__init__` 增加 `denied_skills: Optional[Mapping[str, tuple[str, ...]]] = None` 并保存为 `{user_id: set(skills)}`（key=user_id）。
- 加方法：
```python
    def can_use_skill(self, user: User, workspace_id: str, skill: str) -> bool:
        # default-allow：未被显式 deny 即允许（workspace_id 预留给 adapter 做 per-workspace）
        return skill not in self._denied_skills.get(user.user_id, set())
```
（`__init__` 里：`self._denied_skills = {k: set(v) for k, v in (denied_skills or {}).items()}`。）

- [ ] **Step 4: PASS（3 passed）+ P6b identity 不回归 + purity + ruff**

Run: `.venv/bin/pytest tests/test_can_use_skill.py tests/test_identity_provider.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 新 3 passed + P6b identity 4 passed；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/identity/contracts.py claw_engine/engine/identity/memory.py tests/test_can_use_skill.py
git commit -m "feat: add IdentityProvider.can_use_skill (default-allow, denied-set)"
```

---

### Task 2: SkillSource + LocalDirSkillSource

**Files:**
- Create: `claw_engine/engine/skills/__init__.py`
- Create: `claw_engine/engine/skills/source.py`
- Test: `tests/test_skill_source.py`

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_skill_source.py -q`
Expected: FAIL（ModuleNotFoundError: ...skills.source）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/skills/source.py
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
```

- [ ] **Step 4: PASS（3 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_skill_source.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 8 passed；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/skills/__init__.py claw_engine/engine/skills/source.py tests/test_skill_source.py
git commit -m "feat: add SkillSource + LocalDirSkillSource"
```

---

### Task 3: SkillProvisioner（RBAC 过滤 + 真实同步）+ e2e + 最终回归

**Files:**
- Create: `claw_engine/engine/skills/provisioner.py`
- Test: `tests/test_skill_provisioner.py`

- [ ] **Step 1: 写失败测试（hermetic：tmp source + tmp cwd）**

```python
# tests/test_skill_provisioner.py
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
    # 关键：dest 下不应出现任何穿越路径产物
    for bad in ("evil", "..", ".", "tmp"):
        # 防穿越产物（这些是 join 后可能产生的可疑目录名）
        path = cwd / ".agents" / "skills" / bad
        assert not path.exists()

def test_source_dir_without_skill_md_is_invalid(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    # 在 source 加个无 SKILL.md 的目录
    (tmp_path / "skills_src" / "bad_skill").mkdir()
    prov, user = _provisioner(tmp_path)
    rw = _resolved(cwd, ("abtest", "bad_skill"))
    result = prov.provision(rw, user)
    assert result.provisioned == ("abtest",)
    assert result.invalid == ("bad_skill",)             # 目录在但无 SKILL.md -> invalid（非 missing）
    assert not os.path.exists(cwd / ".agents" / "skills" / "bad_skill")
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_skill_provisioner.py -q`
Expected: FAIL（ModuleNotFoundError: ...skills.provisioner）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/skills/provisioner.py
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
        effective = set(provisioned)
        revoked: list[str] = []
        for prev_skill in sorted(prev_managed - effective):
            target = os.path.join(dest_root, prev_skill)
            if os.path.isdir(target):
                shutil.rmtree(target)
                revoked.append(prev_skill)

        # 写新 manifest（只记录本次本工具实际管理的 skill）
        self._write_manifest(dest_root, effective)

        return ProvisionResult(tuple(provisioned), tuple(denied), tuple(missing),
                               tuple(invalid), tuple(revoked))

    def _manifest_path(self, dest_root: str) -> str:
        return os.path.join(dest_root, _MANIFEST_FILE)

    def _read_manifest(self, dest_root: str) -> set:
        try:
            with open(self._manifest_path(dest_root), encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("managed", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _write_manifest(self, dest_root: str, managed: set) -> None:
        with open(self._manifest_path(dest_root), "w", encoding="utf-8") as f:
            json.dump({"version": _MANIFEST_VERSION, "managed": sorted(managed)}, f)
```

- [ ] **Step 4: PASS（4 passed）+ 最终全量 + ruff + purity（P6d-1 验收）**

Run: `.venv/bin/pytest tests/test_skill_provisioner.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 8 passed；全量 PASS；All checks passed；purity 2 passed（engine/skills 无业务/CLI 字面量）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/skills/provisioner.py tests/test_skill_provisioner.py
git commit -m "feat: add SkillProvisioner (RBAC-filtered sync to .agents/skills)"
```

---

## Self-Review

**1. 决策覆盖：**
- 决策1（拆分，先 6d-1 skill）→ 本 plan 仅 skill，不含 workflow bridge ✅
- 决策2（can_use_skill default-allow + provisioning 时强制）→ Task 1 `test_can_use_skill_default_allow`/`_denied` + Task 3 `test_denied_skill_not_provisioned` ✅
- 决策3（真实同步到 .agents/skills）→ Task 3 `test_provisions_allowed_and_permitted_skills_to_disk`（断言真实文件落盘）✅
- 有效集 = allowed ∩ can_use → Task 3 denied/missing/not-in-allowed 四个用例 ✅
- 边界：何时 provision 不强绑主链路（部署/setup 步骤）；workspace 维度 can_use_skill 留接口 → 设计基线写明 ✅
- 加固A（撤权残留闸口）：manifest `.claw_provisioned.json` 跟踪本工具管理的 skill，本次不再 effective → 当场清理；用户自建目录不动 → Task 3 `test_revoked_skill_is_removed_from_disk` + `test_user_placed_dir_not_in_manifest_is_never_touched` ✅
- 加固B（skill name 防路径穿越）：`validate_skill_name`（白名单 `[A-Za-z0-9_.-]+` + 拒 `.`/`..`）在 LocalDirSkillSource 与 SkillProvisioner 双重防御 → Task 2 `test_invalid_skill_names_rejected_no_read` + Task 3 `test_invalid_skill_name_rejected_no_read_no_write` ✅
- 加固C（SKILL.md 必需 + 区分 missing/invalid）：`has_skill` 要求 SKILL.md 存在；`has_dir` 仅查目录；ProvisionResult 加 `invalid` 桶（malformed） → Task 2 `test_has_skill_false_when_missing_skill_md`/`test_has_dir_distinguishes_malformed_from_missing` + Task 3 `test_source_dir_without_skill_md_is_invalid` ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码（测试片段避免 `a; b` 复合语句以过 ruff E702）。✅

**3. 类型/签名一致性：** `IdentityProvider.can_use_skill(user, workspace_id, skill)->bool`（加进 P6b Protocol，加法式）；`InMemoryIdentityProvider(..., denied_skills=None)`；`SkillSource`(has_skill/skill_dir) + `SkillNotFound`；`LocalDirSkillSource(root)`；`SkillProvisioner(source, identity, *, skills_subdir)` + `provision(workspace, user)->ProvisionResult{provisioned,denied,missing}`；复用 P5 `ResolvedWorkspace`(allowed_skills/cwd/workspace_id)、P6b `User`。各 task 间一致。✅

**已知取舍（实现注意）：**
- RBAC 强制点在 provisioning（只控写不写进 .agents/skills）；CLI 原生跑已 provision 的 skill，引擎不拦运行时调用——故 provisioning 是唯一闸口。
- `can_use_skill` default-allow，workspace_id 参数 V1 InMemory 忽略（denied 按 user_id），留接口给 adapter 做 per-workspace。
- `.agents/skills` 是可配默认参数（不写死 CLI 知识）；purity 不含 codex/claude 字面量。
- copytree 覆盖式（dirs_exist_ok）。**撤权清理**：每次 provision 通过 manifest `cwd/.agents/skills/.claw_provisioned.json` 跟踪本工具管理的 skill，本次不再 effective 的旧 skill **当场从磁盘移除**（写入 result.revoked）——这是 RBAC 闸口的一部分，否则撤权后旧 skill 仍在 .agents/skills 里可被 CLI 读到，闸口失效。manifest 外的用户自建目录绝不动。
- 何时/多久 provision（每会话/启动/定时）= 部署/setup 决策，P6d-1 只交付组件 + hermetic e2e。
- 真实 git skill 源 = adapters 实现 SkillSource，后续。
