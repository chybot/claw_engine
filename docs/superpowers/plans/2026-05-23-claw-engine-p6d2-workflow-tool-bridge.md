# claw_engine P6d-2 — WorkflowToolBridge（run_workflow tool 暴露） 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 给 P4 的 `WorkflowService` 套一层 `WorkflowToolBridge`：agent 通过 tool-call 触发 workflow，bridge 做 RBAC 校验（workspace allowed_workflows ∩ can_run_workflow）、绑定 owner workspace、调用 `WorkflowService.run` 并**立即**返回 `{run_id, status}`。`bridge.get(run_id, principal)` 按 `owner_workspace_id` 做跨 workspace 隔离。MCP/HTTP transport 缓后（部署步骤），P6d-2 只交付 bridge 程序化 API + hermetic 验证。

**Architecture:** `engine/workflows/bridge.py` 放 `WorkflowToolBridge` + `WorkflowNotAllowed` + `ToolInvocationResult`。`Principal{user, workspace_id}` 在 `engine/identity/`。`can_run_workflow` 加到 `IdentityProvider`（mirror `can_use_skill`，default-allow）。`WorkspaceSpec` 与 `ResolvedWorkspace` 加 `allowed_workflows`（mirror `allowed_skills`）。`WorkflowRun` 加 `owner_workspace_id`（防跨 workspace 看 run）。所有改 P3–P6c 既有契约的地方都是**加法式**。

**Tech Stack:** Python 3.11+，标准库，pytest，ruff。全 hermetic（InlineExecutor + 内存 store + FakeIdentity）。

**前置参考：** P4 `WorkflowService`/`WorkflowRun`/`WorkflowStatus`、P5 `WorkspaceSpec`/`ResolvedWorkspace`/`WorkspaceResolver`、P6b `IdentityProvider`/`User`、P6d-1 `can_use_skill` 模型。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策
1. **Principal = `{user: User, workspace_id: str}`**（frozen dataclass），与 P6d-1 skill RBAC 维度一致（user × workspace × workflow）。
2. **`WorkspaceSpec.allowed_workflows`**：mirror `allowed_skills`，per-workspace 白名单；同步加进 `ResolvedWorkspace`。
3. **`can_run_workflow` default-allow**（mirror `can_use_skill`）：有效 = `allowed_workflows ∩ {w : can_run_workflow(user, ws, w)}`。
4. **bridge.invoke 立即返回 `{run_id, status}`**，不阻塞 agent turn；agent 后续调 `bridge.get(run_id, principal)` 查 SUCCESS/FAILED/result。

### 跨 workspace 隔离（owner_workspace_id）
- `WorkflowRun` 加 `owner_workspace_id: Optional[str] = None`（加法式，P4 测试不回归）。
- `WorkflowService.run` 加 `owner_workspace_id` kwarg，原样存进 run。
- `bridge.invoke` 用 `principal.workspace_id` 作为 owner；`bridge.get` 校验 `run.owner_workspace_id == principal.workspace_id`，否则抛 `WorkflowNotAllowed`——防止用其它 workspace 的 run_id 偷看状态。

### 边界（P6d-2 不做）
- **不**实现 MCP server / HTTP transport——让 agent CLI 真的调到 bridge 是部署/集成步骤；P6d-2 只交付程序化 API + hermetic 测试。
- **不**对 workflow 参数做 schema 校验/redaction——params 透传给 handler；handler 自身负责。
- **不**做并发执行隔离（继承 P4 V1 ThreadWorkflowExecutor，无锁）。
- **不**做 workflow 级别的进度脱敏——P6c trace seam 已覆盖，本计划不重复。

### bridge.invoke 流（含 workspace access 闸口）
```
invoke(name, params, principal):
  # 1. workspace 访问（不假设调用方已校验；防越权进入 tool 层）
  if not identity.can_access_workspace(user, ws_id):           raise WorkflowNotAllowed
  ws = resolver.resolve(principal.workspace_id, principal.user.user_id)
  # 2. workspace 白名单
  if name not in ws.allowed_workflows:                         raise WorkflowNotAllowed
  # 3. per-user 工作流权限（default-allow）
  if not identity.can_run_workflow(user, ws_id, name):         raise WorkflowNotAllowed
  # 4. registry 解析（未注册由 WorkflowService.run 抛 WorkflowNotRegistered，不在 bridge 重映射）
  run = workflow_service.run(name, params,
                             owner_workspace_id=principal.workspace_id)
  return ToolInvocationResult(run_id=run.run_id, status=run.status)
```
任一闸口未通过 → 异常抛出、**handler 不跑**、`workflow_service.run` 未被调用。

### bridge.get 流（**读取时重新校验当前权限**，撤权后旧 run 不可读）
```
get(run_id, principal):
  run = workflow_service.get(run_id)              # 不存在 -> WorkflowRunNotFound
  # 撤权语义（与 provisioning 一致）：以下任一不满足都拒绝
  if not identity.can_access_workspace(user, ws_id):                       raise WorkflowNotAllowed
  if run.owner_workspace_id != principal.workspace_id:                     raise WorkflowNotAllowed
  ws = resolver.resolve(principal.workspace_id, principal.user.user_id)
  if run.name not in ws.allowed_workflows:                                 raise WorkflowNotAllowed
  if not identity.can_run_workflow(user, ws_id, run.name):                 raise WorkflowNotAllowed
  return run
```
用户被移出 workspace / 工作流从白名单撤掉 / can_run_workflow 翻 false → **旧 run 不再可读**。

---

### Task 1: Principal + can_run_workflow + allowed_workflows on Spec/Resolved

**Files:**
- Modify: `claw_engine/engine/identity/contracts.py`（加 `Principal` + `can_run_workflow` Protocol）
- Modify: `claw_engine/engine/identity/memory.py`（加 `denied_workflows` 参数 + `can_run_workflow`）
- Modify: `claw_engine/engine/context/workspace.py`（`WorkspaceSpec`/`ResolvedWorkspace` 加 `allowed_workflows`；`WorkspaceResolver.resolve` 拷贝）
- Test: `tests/test_p6d2_contract.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p6d2_contract.py
import dataclasses
import pytest
from claw_engine.engine.identity.contracts import Principal, User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec

def test_principal_is_frozen_with_user_and_workspace():
    p = Principal(user=User(user_id="u1", display_name="Alice"), workspace_id="ws1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.workspace_id = "x"
    assert p.user.user_id == "u1" and p.workspace_id == "ws1"

def test_can_run_workflow_default_allow_and_denied():
    idp = InMemoryIdentityProvider(
        users={"ref-u": User(user_id="u", display_name="A", default_workspace="ws1")},
        authorized={"u": ("ws1",)},
        denied_workflows={"u": ("scary-wf",)},
    )
    u = idp.resolve_user("ref-u")
    assert idp.can_run_workflow(u, "ws1", "scan_logistics") is True   # default-allow
    assert idp.can_run_workflow(u, "ws1", "scary-wf") is False        # explicit deny

def test_workspace_spec_and_resolved_carry_allowed_workflows():
    spec = WorkspaceSpec(allowed_skills=("abtest",), allowed_workflows=("wf2", "wf3"),
                         backend_name="codex", max_rounds=10)
    assert spec.allowed_workflows == ("wf2", "wf3")
    resolver = WorkspaceResolver(
        LayeredConfigProvider(), InMemorySecretProvider(),
        workspaces_root="/srv/ws", specs={"ws1": spec},
    )
    rw = resolver.resolve("ws1")
    assert rw.allowed_workflows == ("wf2", "wf3")
    assert rw.allowed_skills == ("abtest",)                           # 既有不回归

def test_resolved_workspace_default_allowed_workflows_empty():
    resolver = WorkspaceResolver(
        LayeredConfigProvider(), InMemorySecretProvider(), workspaces_root="/srv/ws",
    )
    rw = resolver.resolve("ghost")
    assert rw.allowed_workflows == ()                                 # 默认空 tuple
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_p6d2_contract.py -q`
Expected: FAIL（Principal/can_run_workflow/allowed_workflows 尚不存在）

- [ ] **Step 3: 改实现**

a) `engine/identity/contracts.py`：
- 加 import：`from claw_engine.engine.identity` 内自包含；`User` 已存在。
- 在 `User` 之后加 `Principal`：
```python
@dataclass(frozen=True)
class Principal:
    """RBAC 主体：user × workspace_id。bridge.invoke 用它做权限校验。"""
    user: User
    workspace_id: str
```
- 在 `IdentityProvider` Protocol 末尾加：
```python
    def can_run_workflow(self, user: User, workspace_id: str, workflow: str) -> bool: ...
```

b) `engine/identity/memory.py`：
- `__init__` 加 `denied_workflows: Optional[Mapping[str, tuple[str, ...]]] = None` 并保存为 `{user_id: set}`。
- 加方法：
```python
    def can_run_workflow(self, user: User, workspace_id: str, workflow: str) -> bool:
        # default-allow（mirror can_use_skill）；workspace_id 预留给 adapter 做 per-workspace
        return workflow not in self._denied_workflows.get(user.user_id, set())
```

c) `engine/context/workspace.py`：
- `WorkspaceSpec` 加字段：
```python
    allowed_workflows: tuple[str, ...] = ()
```
- `ResolvedWorkspace` 加字段（dataclass 字段顺序：在 allowed_skills 之后；`repr=False` 不影响）：
```python
    allowed_workflows: tuple[str, ...] = ()
```
- `WorkspaceResolver.resolve` 构造 `ResolvedWorkspace` 时加 `allowed_workflows=spec.allowed_workflows`。
- 同步更新 `ResolvedWorkspace.__repr__` 在尾部加 `allowed_workflows`：
```python
    def __repr__(self) -> str:
        return (f"ResolvedWorkspace(workspace_id={self.workspace_id!r}, cwd={self.cwd!r}, "
                f"env={self.redacted_env()!r}, sensitive_keys={set(self.sensitive_keys)!r}, "
                f"allowed_skills={self.allowed_skills!r}, allowed_workflows={self.allowed_workflows!r}, "
                f"backend_name={self.backend_name!r}, max_rounds={self.max_rounds!r})")
```

- [ ] **Step 4: PASS（4 passed）+ P5/P6b/P6d-1 既有不回归 + purity + ruff**

Run: `.venv/bin/pytest tests/test_p6d2_contract.py tests/test_workspace_resolver.py tests/test_user_config_layer.py tests/test_identity_provider.py tests/test_can_use_skill.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 新 4 passed + 既有不回归；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/identity/contracts.py claw_engine/engine/identity/memory.py claw_engine/engine/context/workspace.py tests/test_p6d2_contract.py
git commit -m "feat: add Principal, can_run_workflow, allowed_workflows in WorkspaceSpec/ResolvedWorkspace"
```

---

### Task 2: WorkflowRun.owner_workspace_id + WorkflowService.run kwarg

**Files:**
- Modify: `claw_engine/engine/workflows/contracts.py`（`WorkflowRun` 加 `owner_workspace_id`）
- Modify: `claw_engine/engine/workflows/service.py`（`run` 接受 `owner_workspace_id`）
- Test: `tests/test_workflow_owner.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_owner.py
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService

def _svc():
    reg = EngineRegistry()
    reg.register_workflow("wf", lambda p, c: "ok")
    return WorkflowService(reg, MemoryWorkflowStore())

def test_run_records_owner_workspace_id():
    svc = _svc()
    run = svc.run("wf", {}, owner_workspace_id="ws1")
    assert run.owner_workspace_id == "ws1"

def test_run_default_owner_is_none():
    svc = _svc()
    run = svc.run("wf", {})
    assert run.owner_workspace_id is None        # 加法式：旧调用不传 owner

def test_rerun_preserves_owner():
    svc = _svc()
    first = svc.run("wf", {}, owner_workspace_id="ws1")
    second = svc.rerun(first.run_id)
    assert second.owner_workspace_id == "ws1"    # 撤权后再跑仍记原 owner
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workflow_owner.py -q`
Expected: FAIL（owner_workspace_id 字段/参数 不存在）

- [ ] **Step 3: 改实现**

a) `engine/workflows/contracts.py` `WorkflowRun` 加字段（保留 frozen 与既有 with_*）：
```python
    owner_workspace_id: Optional[str] = None
```
（放在 `rerun_of` 之后或 `updated_at` 之前；不影响既有 with_* 方法，因为 `replace()` 会保留未指定字段。）

b) `engine/workflows/service.py` `WorkflowService.run` 加 kwarg 并传进 `WorkflowRun`：
```python
    def run(self, name: str, params: Optional[Mapping[str, Any]] = None, *,
            rerun_of: Optional[str] = None,
            owner_workspace_id: Optional[str] = None) -> WorkflowRun:
        handler = self._registry.resolve_workflow(name)
        run = WorkflowRun(run_id=uuid.uuid4().hex, name=name, params=dict(params or {}),
                          status=WorkflowStatus.PENDING, rerun_of=rerun_of,
                          owner_workspace_id=owner_workspace_id)
        ...
```
`rerun(run_id)` 把旧 run 的 owner_workspace_id 透传：
```python
    def rerun(self, run_id: str) -> WorkflowRun:
        old = self._store.get(run_id)
        if old.status not in (WorkflowStatus.SUCCESS, WorkflowStatus.FAILED):
            raise WorkflowNotTerminal(run_id, old.status)
        return self.run(old.name, old.params, rerun_of=run_id,
                        owner_workspace_id=old.owner_workspace_id)
```

- [ ] **Step 4: PASS（3 passed）+ P4 workflow 既有不回归 + purity + ruff**

Run: `.venv/bin/pytest tests/test_workflow_owner.py tests/test_workflow_model.py tests/test_workflow_service.py tests/test_workflow_rerun.py tests/test_workflow_thread_executor.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 新 3 passed + P4 workflow 既有不回归；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/contracts.py claw_engine/engine/workflows/service.py tests/test_workflow_owner.py
git commit -m "feat: WorkflowRun.owner_workspace_id + WorkflowService.run kwarg (cross-workspace isolation prep)"
```

---

### Task 3: WorkflowToolBridge（invoke + get + RBAC + 跨 workspace 隔离）+ 最终回归

**Files:**
- Create: `claw_engine/engine/workflows/bridge.py`
- Test: `tests/test_workflow_tool_bridge.py`

- [ ] **Step 1: 写失败测试（含权限通过/拒绝/owner 隔离/handler 不跑等）**

```python
# tests/test_workflow_tool_bridge.py
import itertools
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
from claw_engine.engine.identity.contracts import Principal, User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.workflows.bridge import (
    ToolInvocationResult, WorkflowNotAllowed, WorkflowToolBridge,
)
from claw_engine.engine.workflows.contracts import WorkflowStatus
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService


def _build_with_handles(*, denied_workflows=None, allowed_workflows=("wf2",),
                        spec_ws="ws1", authorized=("ws1",)):
    """返回所有 handles（供需要在 invoke 与 get 之间改 identity/resolver 状态的撤权测试用）。"""
    counter = itertools.count(1)
    calls = {"n": 0}

    def handler(params, ctx):
        calls["n"] += 1
        return {"call": next(counter), "params": dict(params)}

    reg = EngineRegistry()
    reg.register_workflow("wf2", handler)
    reg.register_workflow("wf3", handler)
    service = WorkflowService(reg, MemoryWorkflowStore())   # InlineExecutor 默认，invoke 后立刻终态

    resolver = WorkspaceResolver(
        LayeredConfigProvider(), InMemorySecretProvider(),
        workspaces_root="/srv/ws",
        specs={spec_ws: WorkspaceSpec(allowed_workflows=allowed_workflows)},
    )
    identity = InMemoryIdentityProvider(
        users={"ref-u": User(user_id="u", display_name="A", default_workspace=spec_ws)},
        authorized={"u": tuple(authorized)},
        denied_workflows={"u": tuple(denied_workflows or ())},
    )
    bridge = WorkflowToolBridge(service, identity, resolver)
    user = identity.resolve_user("ref-u")
    return bridge, user, calls, service, resolver, identity


def _build(**kwargs):
    bridge, user, calls, *_ = _build_with_handles(**kwargs)
    return bridge, user, calls

def test_invoke_runs_allowed_workflow_and_returns_run_id_status():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {"x": 1}, Principal(user=user, workspace_id="ws1"))
    assert isinstance(res, ToolInvocationResult)
    assert res.run_id and res.status is WorkflowStatus.SUCCESS   # Inline executor 已跑完
    assert calls["n"] == 1

def test_invoke_workflow_not_in_allowed_raises_and_handler_not_called():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    with pytest.raises(WorkflowNotAllowed) as ei:
        bridge.invoke("wf3", {}, Principal(user=user, workspace_id="ws1"))  # wf3 不在白名单
    assert ei.value.workflow == "wf3" and ei.value.workspace_id == "ws1"
    assert calls["n"] == 0                                       # handler 没跑

def test_invoke_can_run_workflow_denied_raises_and_handler_not_called():
    bridge, user, calls = _build(allowed_workflows=("wf2",), denied_workflows=("wf2",))
    with pytest.raises(WorkflowNotAllowed):
        bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))   # 在白名单但被 deny
    assert calls["n"] == 0

def test_invoke_binds_owner_workspace_id_on_run():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    run = bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))
    assert run.owner_workspace_id == "ws1"                       # owner 绑定

def test_get_returns_own_run():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {"k": "v"}, Principal(user=user, workspace_id="ws1"))
    run = bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))
    assert run.status is WorkflowStatus.SUCCESS
    assert run.result["params"] == {"k": "v"}

def test_get_cross_workspace_raises_not_allowed():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    # 用其它 workspace 的 principal 试图查这个 run
    other_user = User(user_id="u2", display_name="B", default_workspace="ws2")
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=other_user, workspace_id="ws2"))

# --- 新增加固 ---

def test_invoke_workspace_access_denied_handler_not_called():
    # 用户未被授权访问该 workspace（authorized=()）-> 第一闸口拦截，handler 不跑
    bridge, user, calls = _build(authorized=())
    with pytest.raises(WorkflowNotAllowed):
        bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    assert calls["n"] == 0

def test_invoke_unregistered_workflow_propagates_not_remapped():
    """workflow 在 workspace 白名单但 registry 未注册：bridge 不重映射，让 WorkflowNotRegistered 透传。"""
    from claw_engine.engine.bootstrap import WorkflowNotRegistered
    bridge, user, calls = _build(allowed_workflows=("ghost",))   # ghost 未注册 handler
    with pytest.raises(WorkflowNotRegistered):
        bridge.invoke("ghost", {}, Principal(user=user, workspace_id="ws1"))
    assert calls["n"] == 0   # 已注册的 wf2/wf3 handler 也未被错误调用

def test_get_after_workspace_access_revoked():
    """invoke 时用户在 workspace；之后被移出授权 -> get 不再可读旧 run（撤权一致语义）。"""
    bridge, user, calls, service, resolver, identity = _build_with_handles()
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    identity._authorized["u"] = ()                                # 撤权（V1 内存实现直接改）
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))

def test_get_after_workflow_removed_from_allowed():
    """工作流从 workspace 白名单撤掉 -> 旧 run 不再可读。"""
    bridge, user, calls, service, resolver, identity = _build_with_handles(
        allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    resolver._specs["ws1"] = WorkspaceSpec(allowed_workflows=())   # 白名单清空
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))

def test_get_after_can_run_workflow_revoked():
    """can_run_workflow 翻 false -> 旧 run 不再可读。"""
    bridge, user, calls, service, resolver, identity = _build_with_handles()
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    identity._denied_workflows["u"] = {"wf2"}                      # deny 当前工作流
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workflow_tool_bridge.py -q`
Expected: FAIL（ModuleNotFoundError: ...workflows.bridge）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/workflows/bridge.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.identity.contracts import IdentityProvider, Principal
from claw_engine.engine.workflows.contracts import WorkflowRun, WorkflowStatus
from claw_engine.engine.workflows.service import WorkflowService


class WorkflowNotAllowed(PermissionError):
    """workflow 不在 workspace 白名单 / 被用户 can_run_workflow 拒绝 / 跨 workspace 访问。"""
    def __init__(self, workspace_id: str, workflow: str) -> None:
        super().__init__(f"workflow {workflow!r} 在 workspace {workspace_id!r} 不被允许")
        self.workspace_id = workspace_id
        self.workflow = workflow


@dataclass(frozen=True)
class ToolInvocationResult:
    """bridge.invoke 的即时返回：仅 run_id + 当前 status，不阻塞 agent turn。"""
    run_id: str
    status: WorkflowStatus


class WorkflowToolBridge:
    """把 WorkflowService 包装成 agent 可调用的 tool；做 RBAC 与跨 workspace 隔离。"""

    def __init__(self, workflow_service: WorkflowService, identity: IdentityProvider,
                 resolver: WorkspaceResolver) -> None:
        self._service = workflow_service
        self._identity = identity
        self._resolver = resolver

    def invoke(self, name: str, params: Optional[Mapping[str, Any]],
               principal: Principal) -> ToolInvocationResult:
        # 1. workspace 访问（不假设上层已校验）
        if not self._identity.can_access_workspace(principal.user, principal.workspace_id):
            raise WorkflowNotAllowed(principal.workspace_id, name)
        ws = self._resolver.resolve(principal.workspace_id, principal.user.user_id)
        # 2. workspace 白名单
        if name not in ws.allowed_workflows:
            raise WorkflowNotAllowed(principal.workspace_id, name)
        # 3. per-user 工作流权限（default-allow）
        if not self._identity.can_run_workflow(principal.user, principal.workspace_id, name):
            raise WorkflowNotAllowed(principal.workspace_id, name)
        # 4. 注册解析由 WorkflowService 抛 WorkflowNotRegistered（不在 bridge 层重映射）
        run = self._service.run(name, params, owner_workspace_id=principal.workspace_id)
        return ToolInvocationResult(run_id=run.run_id, status=run.status)

    def get(self, run_id: str, principal: Principal) -> WorkflowRun:
        """读取时重新校验当前权限（撤权语义与 provisioning 一致）。"""
        run = self._service.get(run_id)                          # 不存在抛 WorkflowRunNotFound
        if not self._identity.can_access_workspace(principal.user, principal.workspace_id):
            raise WorkflowNotAllowed(principal.workspace_id, run.name)
        if run.owner_workspace_id != principal.workspace_id:
            raise WorkflowNotAllowed(principal.workspace_id, run.name)   # 跨 ws 隔离
        ws = self._resolver.resolve(principal.workspace_id, principal.user.user_id)
        if run.name not in ws.allowed_workflows:
            raise WorkflowNotAllowed(principal.workspace_id, run.name)   # 工作流已撤
        if not self._identity.can_run_workflow(principal.user, principal.workspace_id, run.name):
            raise WorkflowNotAllowed(principal.workspace_id, run.name)   # can_run_workflow 撤权
        return run
```

- [ ] **Step 4: PASS（6 passed）+ 最终全量 + ruff + purity（P6d-2 验收）**

Run: `.venv/bin/pytest tests/test_workflow_tool_bridge.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 11 passed；全量 PASS；All checks passed；purity 2 passed（engine/workflows/bridge 无业务/CLI 字面量）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/bridge.py tests/test_workflow_tool_bridge.py
git commit -m "feat: add WorkflowToolBridge (RBAC + owner_workspace_id isolation)"
```

---

## Self-Review

**1. 决策覆盖：**
- 决策1（Principal{user, workspace_id}）→ Task 1 `test_principal_is_frozen_with_user_and_workspace` ✅
- 决策2（WorkspaceSpec.allowed_workflows）→ Task 1 `test_workspace_spec_and_resolved_carry_allowed_workflows` ✅
- 决策3（can_run_workflow default-allow）→ Task 1 `test_can_run_workflow_default_allow_and_denied` ✅
- 决策4（invoke 返 run_id+status 立即上）→ Task 3 `test_invoke_runs_allowed_workflow_and_returns_run_id_status` ✅
- RBAC 双层（workspace allowed_workflows ∩ can_run_workflow）→ Task 3 `test_invoke_workflow_not_in_allowed_raises` + `test_invoke_can_run_workflow_denied_raises`（两条路径独立验证）✅
- 拒绝时 handler 不跑（spy.calls==0）→ Task 3 上述两条都断言 ✅
- 跨 workspace 隔离 → Task 3 `test_invoke_binds_owner_workspace_id` + `test_get_cross_workspace_raises_not_allowed` ✅
- 向后兼容（P3/P4/P5/P6b/P6c/P6d-1 不回归）→ Task 1/Task 2 加法式签名 + Task 3 Step 4 跑既有套件 ✅
- 加固A（invoke 显式 can_access_workspace）→ Task 3 `test_invoke_workspace_access_denied_handler_not_called`（handler.calls==0）✅
- 加固B（get 撤权重校验：access/owner/allowed_workflows/can_run_workflow 任一不满足都拒）→ Task 3 `test_get_after_workspace_access_revoked` + `test_get_after_workflow_removed_from_allowed` + `test_get_after_can_run_workflow_revoked` ✅
- 加固C（未注册 workflow 透传 WorkflowNotRegistered，不在 bridge 重映射）→ Task 3 `test_invoke_unregistered_workflow_propagates_not_remapped`（calls==0）✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码（测试片段避免 `a; b` 复合语句以过 ruff E702）。✅

**3. 类型/签名一致性：** `Principal{user: User, workspace_id: str}`；`IdentityProvider.can_run_workflow(user, ws, workflow)->bool`；`InMemoryIdentityProvider(..., denied_workflows=None)`；`WorkspaceSpec.allowed_workflows: tuple[str,...]=()`；`ResolvedWorkspace.allowed_workflows: tuple[str,...]=()`；`WorkflowRun.owner_workspace_id: Optional[str]=None`；`WorkflowService.run(..., owner_workspace_id=None)`；`WorkflowToolBridge(service, identity, resolver).invoke(name, params, principal)->ToolInvocationResult{run_id, status}`/`get(run_id, principal)->WorkflowRun`；`WorkflowNotAllowed(workspace_id, workflow)`。各 task 间一致。✅

**已知取舍（实现注意）：**
- bridge.invoke 立即返回（InlineExecutor 默认下其实跑完了，但 status 仍如实反映；ThreadExecutor 下会真 PENDING/RUNNING）。
- bridge.get **每次都重新校验当前权限**（access + owner + allowed_workflows + can_run_workflow），任一不满足即拒——与 P6d-1 provisioning 撤权一致：用户被移出 workspace、工作流从白名单撤掉、can_run_workflow 翻 false 后，**旧 run 不再可读**。
- 同 workspace 内的不同用户可见同一 run（V1 不做 user 级 run 隔离）；但**前提是该用户当前仍满足 access + workflow 权限**——撤权后即丢失可读性。
- 未注册 workflow 抛 `WorkflowNotRegistered`（来自 WorkflowService），bridge 不重映射成 WorkflowNotAllowed；这保留调试可见性。
- MCP/HTTP transport 缓后（adapter 部署步骤）；P6d-2 仅程序化 API。
- workflow 参数不做 schema 校验/redaction（handler 自身责任）；P6c trace seam 已覆盖运行时脱敏。
- ResolvedWorkspace.__repr__ 同步加 `allowed_workflows`；安全（P5 加固）：repr 仍只显脱敏 env，allowed_workflows 不是 secret，明文展示安全。
