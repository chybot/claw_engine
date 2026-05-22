# claw_engine P4 — Workflow 引擎（run_workflow contract）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 落地业务无关的轻量 workflow 引擎：`run_workflow(name, params)` 把一个**注册的 handler** 当后台任务跑，引擎拥有任务生命周期（PENDING→RUNNING→SUCCESS/FAILED）、进度事件、按 run_id 查询、rerun（新 run_id 保留历史）。

**Architecture:** L3 `WorkflowService`（拥有 run 生命周期）+ `WorkflowStore`（抽象 Protocol + memory）+ `WorkflowExecutor`（抽象，默认 `InlineExecutor` 同步，另有 `ThreadWorkflowExecutor`）。`WorkflowRun`/`ProgressEvent` 不可变（frozen，`with_*` 产新副本）。handler 由 adapters 注册（像 backend 一样可插拔），handler body 是业务、引擎不认。run_workflow 暴露成「给 agent 的 tool」属 channel/MCP 集成（后续）；P4 只做引擎 API。

**Tech Stack:** Python 3.11+，标准库 `uuid`/`concurrent.futures`/`threading`，pytest，ruff。全 hermetic（fake handler + InlineExecutor；线程测试用 event 同步，确定性）。

**前置参考：** spec §2（L3）、roadmap，P1–P3 计划。已拍板决策见下。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策
1. **P4 只做 workflow 引擎**；skill runtime/provisioning 移到 P5（skill 是 workspace 作用域，与 L4 同层更内聚）。
2. **workflow = 注册的 handler**（`name → callable(params, ctx) -> result`）；引擎拥有 `WorkflowRun{run_id,name,params,status,progress[],result,error,rerun_of}` 全生命周期；handler 由 adapters 注册，body 是业务。
3. **执行模型 = WorkflowExecutor 抽象 + InlineExecutor 默认**（同步、确定性、便于 hermetic 测试）；另提供 `ThreadWorkflowExecutor`（后台）。
4. **rerun = 新 run_id 的新运行**（`rerun_of` 指向旧 run），旧 run 记录保留（不可变、可审计）。

### 边界（P4 不做）
- **不**做 skill runtime / SKILL.md 装载（P5）。
- **不**把 run_workflow 接成「backend 可调用的 tool」（需 MCP/tool-bridge，属 channel/集成，后续）；P4 只提供 `WorkflowService` 程序化 API。
- **不**做 workflow 间依赖/DAG 编排（spec §9 已定 V1 轻量，DAG 延后）。
- **不**做 workflow 持久化到 sqlite（V1 用 MemoryWorkflowStore；sqlite 与 SessionStore 一样可后续加，Protocol 已留口）。
- **不**做并发去重/分布式（单机；ThreadExecutor 仅本机线程池）。

### 关键语义
- **handler 失败归一**：handler 抛任何 `Exception` → run 置 `FAILED`，`error=f"{type}: {msg}"`，**不向引擎上层冒泡**（workflow 任务失败是正常业务结果，不是引擎崩溃）。`BaseException`（如 KeyboardInterrupt）不捕获。
  - 注意与 backend 的**窄异常**对比：backend 里我们用窄 except 避免掩盖**自己代码**的 bug；这里 handler 是**业务代码**，任何失败都应成为 FAILED run，故用 `except Exception`。
- **进度**：handler 通过 `ctx.report_progress(message, step=, fraction=)` 追加 `ProgressEvent`；引擎按不可变方式 append 到 run.progress。失败前已报的进度保留。
- **inline vs background**：InlineExecutor 下 `run()` 返回时 run 已终态；ThreadExecutor 下 `run()` 可能返回 PENDING/RUNNING。返回值 = 当前 store 中的 run 快照。
- **rerun**：取旧 run 的 name+params，开新 run_id 重跑；旧 run 不变。

---

### Task 1: Workflow 契约类型

**Files:**
- Create: `claw_engine/engine/workflows/__init__.py`
- Create: `claw_engine/engine/workflows/contracts.py`
- Test: `tests/test_workflow_model.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_model.py
import dataclasses
import pytest
from claw_engine.engine.workflows.contracts import (
    WorkflowRun, WorkflowStatus, ProgressEvent,
)

def _run(**kw):
    base = dict(run_id="r1", name="wf", params={"a": 1})
    base.update(kw)
    return WorkflowRun(**base)

def test_run_is_frozen():
    r = _run()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = WorkflowStatus.RUNNING

def test_defaults():
    r = _run()
    assert r.status is WorkflowStatus.PENDING
    assert r.progress == ()
    assert r.result is None and r.error is None and r.rerun_of is None

def test_with_status_and_progress_are_immutable():
    r = _run()
    r2 = r.with_status(WorkflowStatus.RUNNING, now=1.0)
    assert r.status is WorkflowStatus.PENDING       # 原不变
    assert r2.status is WorkflowStatus.RUNNING and r2.updated_at == 1.0
    ev = ProgressEvent(message="step1", step="s1", fraction=0.5, ts=2.0)
    r3 = r2.with_progress(ev, now=2.0)
    assert r2.progress == ()
    assert r3.progress == (ev,)

def test_with_result_sets_success_and_with_error_sets_failed():
    r = _run().with_result({"ok": True}, now=3.0)
    assert r.status is WorkflowStatus.SUCCESS and r.result == {"ok": True}
    r2 = _run().with_error("BoomError: bad", now=4.0)
    assert r2.status is WorkflowStatus.FAILED and r2.error == "BoomError: bad"

def test_terminal_fields_are_mutually_exclusive():
    recovered = _run().with_error("boom").with_result({"ok": True})
    assert recovered.result == {"ok": True} and recovered.error is None   # 成功清掉 error
    failed = _run().with_result({"ok": True}).with_error("boom")
    assert failed.error == "boom" and failed.result is None               # 失败清掉 result
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workflow_model.py -q`
Expected: FAIL（ModuleNotFoundError: ...workflows.contracts）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/workflows/contracts.py
from __future__ import annotations
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressEvent:
    message: str
    step: Optional[str] = None
    fraction: Optional[float] = None   # 0.0..1.0
    ts: float = 0.0


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    name: str
    params: Mapping[str, Any]
    status: WorkflowStatus = WorkflowStatus.PENDING
    progress: tuple[ProgressEvent, ...] = ()
    result: Optional[Any] = None
    error: Optional[str] = None
    rerun_of: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @staticmethod
    def _now(now: Optional[float]) -> float:
        return now if now is not None else time.time()

    def with_status(self, status: WorkflowStatus, *, now: Optional[float] = None) -> "WorkflowRun":
        return replace(self, status=status, updated_at=self._now(now))

    def with_progress(self, event: ProgressEvent, *, now: Optional[float] = None) -> "WorkflowRun":
        return replace(self, progress=self.progress + (event,), updated_at=self._now(now))

    def with_result(self, result: Any, *, now: Optional[float] = None) -> "WorkflowRun":
        # 终态字段互斥：成功时清掉旧 error
        return replace(self, status=WorkflowStatus.SUCCESS, result=result, error=None,
                       updated_at=self._now(now))

    def with_error(self, error: str, *, now: Optional[float] = None) -> "WorkflowRun":
        # 终态字段互斥：失败时清掉旧 result
        return replace(self, status=WorkflowStatus.FAILED, error=error, result=None,
                       updated_at=self._now(now))


@runtime_checkable
class WorkflowStore(Protocol):
    def save(self, run: WorkflowRun) -> None: ...
    def get(self, run_id: str) -> WorkflowRun: ...


@runtime_checkable
class WorkflowExecutor(Protocol):
    def submit(self, fn: Callable[[], None]) -> None: ...


@dataclass
class WorkflowContext:
    """传给 handler：用于上报进度。"""
    run_id: str
    store: WorkflowStore

    def report_progress(self, message: str, *, step: Optional[str] = None,
                        fraction: Optional[float] = None) -> None:
        if fraction is not None and not 0.0 <= fraction <= 1.0:
            raise ValueError(f"fraction 必须在 0.0..1.0 之间，得到 {fraction}")
        run = self.store.get(self.run_id)
        self.store.save(run.with_progress(
            ProgressEvent(message=message, step=step, fraction=fraction, ts=time.time())))


# handler(params, ctx) -> result；抛 Exception 表示任务失败
WorkflowHandler = Callable[[Mapping[str, Any], WorkflowContext], Any]
```

- [ ] **Step 4: 跑确认 PASS（4 passed）+ purity**

Run: `.venv/bin/pytest tests/test_workflow_model.py -q && .venv/bin/pytest tests/purity -q`
Expected: 5 passed；purity 2 passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/__init__.py claw_engine/engine/workflows/contracts.py tests/test_workflow_model.py
git commit -m "feat: add workflow contract types (WorkflowRun, ProgressEvent, executor/store/handler)"
```

---

### Task 2: MemoryWorkflowStore + InlineExecutor + Registry 扩展

**Files:**
- Create: `claw_engine/engine/workflows/memory_store.py`
- Create: `claw_engine/engine/workflows/executor.py`
- Modify: `claw_engine/engine/bootstrap.py`（加 register_workflow/resolve_workflow）
- Test: `tests/test_workflow_store_executor.py`, `tests/test_registry.py`（追加 workflow 注册用例）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_store_executor.py
import pytest
from claw_engine.engine.workflows.contracts import WorkflowRun, WorkflowStatus
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore, WorkflowRunNotFound
from claw_engine.engine.workflows.executor import InlineExecutor

def test_store_save_and_get():
    store = MemoryWorkflowStore()
    run = WorkflowRun(run_id="r1", name="wf", params={})
    store.save(run)
    assert store.get("r1").run_id == "r1"
    store.save(run.with_status(WorkflowStatus.RUNNING))
    assert store.get("r1").status is WorkflowStatus.RUNNING   # upsert

def test_store_get_missing_raises():
    with pytest.raises(WorkflowRunNotFound):
        MemoryWorkflowStore().get("nope")

def test_inline_executor_runs_immediately():
    calls = []
    InlineExecutor().submit(lambda: calls.append(1))
    assert calls == [1]
```

追加到 `tests/test_registry.py`：
```python
def test_register_and_resolve_workflow():
    from claw_engine.engine.bootstrap import EngineRegistry, WorkflowNotRegistered
    reg = EngineRegistry()
    reg.register_workflow("wf", lambda params, ctx: "ok")
    assert reg.resolve_workflow("wf")({}, None) == "ok"
    import pytest
    with pytest.raises(WorkflowNotRegistered):
        reg.resolve_workflow("missing")
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workflow_store_executor.py tests/test_registry.py -q`
Expected: FAIL（ModuleNotFoundError + WorkflowNotRegistered 未定义）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/workflows/memory_store.py
from __future__ import annotations
from typing import Dict
from claw_engine.engine.workflows.contracts import WorkflowRun


class WorkflowRunNotFound(KeyError):
    pass


class MemoryWorkflowStore:
    def __init__(self) -> None:
        self._runs: Dict[str, WorkflowRun] = {}

    def save(self, run: WorkflowRun) -> None:
        self._runs[run.run_id] = run

    def get(self, run_id: str) -> WorkflowRun:
        try:
            return self._runs[run_id]
        except KeyError:
            raise WorkflowRunNotFound(run_id) from None
```

```python
# claw_engine/engine/workflows/executor.py
from __future__ import annotations
from typing import Callable


class InlineExecutor:
    """同步立即执行——确定性，默认实现。"""

    def submit(self, fn: Callable[[], None]) -> None:
        fn()
```

`claw_engine/engine/bootstrap.py` 增量（保留现有 backend 部分，新增）：
```python
# 顶部 import 增加：
from claw_engine.engine.workflows.contracts import WorkflowHandler

# 新增异常：
class WorkflowNotRegistered(KeyError):
    pass

# EngineRegistry.__init__ 增加：
        self._workflows: Dict[str, WorkflowHandler] = {}

# EngineRegistry 增加两个方法：
    def register_workflow(self, name: str, handler: WorkflowHandler) -> None:
        self._workflows[name] = handler

    def resolve_workflow(self, name: str) -> WorkflowHandler:
        try:
            return self._workflows[name]
        except KeyError:
            raise WorkflowNotRegistered(name) from None
```
（`Dict` 已在 bootstrap import；若无则补 `from typing import Callable, Dict`。）

- [ ] **Step 4: 跑确认 PASS + 全量 + ruff + purity**

Run: `.venv/bin/pytest tests/test_workflow_store_executor.py tests/test_registry.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 新测试 PASS；全量 PASS；All checks passed；purity 2 passed（bootstrap import workflows.contracts 属 engine 内部，无 CLI 字面量）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/memory_store.py claw_engine/engine/workflows/executor.py claw_engine/engine/bootstrap.py tests/test_workflow_store_executor.py tests/test_registry.py
git commit -m "feat: add MemoryWorkflowStore, InlineExecutor, registry workflow registration"
```

---

### Task 3: WorkflowService.run（inline 生命周期 + 进度 + 失败归一）

**Files:**
- Create: `claw_engine/engine/workflows/service.py`
- Test: `tests/test_workflow_service.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_service.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry, WorkflowNotRegistered
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService
from claw_engine.engine.workflows.contracts import WorkflowStatus


def _service(handlers):
    reg = EngineRegistry()
    for name, fn in handlers.items():
        reg.register_workflow(name, fn)
    return WorkflowService(reg, MemoryWorkflowStore())   # 默认 InlineExecutor

def test_run_success_captures_result_and_progress():
    def ok(params, ctx):
        ctx.report_progress("start", step="s1", fraction=0.0)
        ctx.report_progress("done", step="s2", fraction=1.0)
        return {"echo": params["x"]}
    svc = _service({"ok": ok})
    run = svc.run("ok", {"x": 42})
    assert run.status is WorkflowStatus.SUCCESS
    assert run.result == {"echo": 42}
    assert [p.message for p in run.progress] == ["start", "done"]
    assert run.progress[1].fraction == 1.0

def test_run_failure_records_error_and_keeps_progress():
    def boom(params, ctx):
        ctx.report_progress("before crash")
        raise ValueError("bad input")
    svc = _service({"boom": boom})
    run = svc.run("boom", {})
    assert run.status is WorkflowStatus.FAILED
    assert "ValueError" in run.error and "bad input" in run.error
    assert [p.message for p in run.progress] == ["before crash"]   # 失败前进度保留

def test_run_unregistered_raises():
    svc = _service({})
    with pytest.raises(WorkflowNotRegistered):
        svc.run("missing", {})

def test_get_returns_run():
    svc = _service({"ok": lambda params, ctx: "done"})
    run = svc.run("ok", {})
    assert svc.get(run.run_id).run_id == run.run_id
    assert svc.get(run.run_id).result == "done"

def test_invalid_fraction_fails_run():
    def bad(params, ctx):
        ctx.report_progress("oops", fraction=1.5)   # 越界 → ValueError → 归一 FAILED
        return "unreachable"
    svc = _service({"bad": bad})
    run = svc.run("bad", {})
    assert run.status is WorkflowStatus.FAILED
    assert "fraction" in run.error
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workflow_service.py -q`
Expected: FAIL（ModuleNotFoundError: ...workflows.service）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/workflows/service.py
from __future__ import annotations
import uuid
from typing import Any, Mapping, Optional
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.contracts import (
    WorkflowContext, WorkflowExecutor, WorkflowHandler, WorkflowRun, WorkflowStatus, WorkflowStore,
)
from claw_engine.engine.workflows.executor import InlineExecutor


class WorkflowService:
    def __init__(self, registry: EngineRegistry, store: WorkflowStore,
                 executor: Optional[WorkflowExecutor] = None) -> None:
        self._registry = registry
        self._store = store
        self._executor = executor or InlineExecutor()

    def run(self, name: str, params: Optional[Mapping[str, Any]] = None, *,
            rerun_of: Optional[str] = None) -> WorkflowRun:
        handler = self._registry.resolve_workflow(name)   # 未注册即抛 WorkflowNotRegistered
        run = WorkflowRun(run_id=uuid.uuid4().hex, name=name, params=dict(params or {}),
                          status=WorkflowStatus.PENDING, rerun_of=rerun_of)
        self._store.save(run)
        self._executor.submit(lambda: self._execute(run.run_id, handler))
        return self._store.get(run.run_id)

    def get(self, run_id: str) -> WorkflowRun:
        return self._store.get(run_id)

    def _execute(self, run_id: str, handler: WorkflowHandler) -> None:
        self._store.save(self._store.get(run_id).with_status(WorkflowStatus.RUNNING))
        ctx = WorkflowContext(run_id=run_id, store=self._store)
        params = self._store.get(run_id).params
        try:
            result = handler(params, ctx)
        except Exception as exc:   # 业务 handler 失败 = 任务 FAILED（归一，不向上冒泡）
            self._store.save(self._store.get(run_id).with_error(f"{type(exc).__name__}: {exc}"))
            return
        self._store.save(self._store.get(run_id).with_result(result))
```

- [ ] **Step 4: 跑确认 PASS（4 passed）+ 全量 + ruff + purity**

Run: `.venv/bin/pytest tests/test_workflow_service.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 5 passed；全量 PASS；All checks passed；purity 2 passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/service.py tests/test_workflow_service.py
git commit -m "feat: add WorkflowService.run (lifecycle, progress capture, failure normalization)"
```

---

### Task 4: rerun（新 run_id 保留历史）

**Files:**
- Modify: `claw_engine/engine/workflows/service.py`（加 `rerun`）
- Test: `tests/test_workflow_rerun.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_rerun.py
import itertools
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService, WorkflowNotTerminal
from claw_engine.engine.workflows.contracts import WorkflowRun, WorkflowStatus


def test_rerun_creates_new_run_preserving_history():
    counter = itertools.count(1)
    def wf(params, ctx):
        return {"call": next(counter)}
    reg = EngineRegistry(); reg.register_workflow("wf", wf)
    store = MemoryWorkflowStore()
    svc = WorkflowService(reg, store)

    first = svc.run("wf", {"k": "v"})
    assert first.result == {"call": 1}

    second = svc.rerun(first.run_id)
    assert second.run_id != first.run_id          # 新 run_id
    assert second.rerun_of == first.run_id        # 指向旧 run
    assert second.name == "wf" and second.params == {"k": "v"}   # 同 name+params
    assert second.result == {"call": 2}           # 独立的新一次运行

    # 旧 run 记录保留、不被改动
    old = svc.get(first.run_id)
    assert old.run_id == first.run_id
    assert old.result == {"call": 1}
    assert old.rerun_of is None
    assert old.status is WorkflowStatus.SUCCESS

def test_rerun_of_failed_run_can_succeed():
    state = {"fail_first": True}
    def flaky(params, ctx):
        if state["fail_first"]:
            state["fail_first"] = False
            raise RuntimeError("first fails")
        return "ok"
    reg = EngineRegistry(); reg.register_workflow("flaky", flaky)
    svc = WorkflowService(reg, MemoryWorkflowStore())
    first = svc.run("flaky", {})
    assert first.status is WorkflowStatus.FAILED
    second = svc.rerun(first.run_id)
    assert second.status is WorkflowStatus.SUCCESS and second.result == "ok"
    assert svc.get(first.run_id).status is WorkflowStatus.FAILED   # 旧 run 仍为失败

def test_rerun_non_terminal_run_raises():
    reg = EngineRegistry(); reg.register_workflow("wf", lambda p, c: "ok")
    store = MemoryWorkflowStore()
    svc = WorkflowService(reg, store)
    # 手工塞一个 RUNNING（非终态）run
    store.save(WorkflowRun(run_id="running1", name="wf", params={}, status=WorkflowStatus.RUNNING))
    with pytest.raises(WorkflowNotTerminal):
        svc.rerun("running1")
```

- [ ] **Step 2: 跑确认 FAIL（AttributeError: WorkflowService 无 rerun）**

Run: `.venv/bin/pytest tests/test_workflow_rerun.py -q`
Expected: FAIL

- [ ] **Step 3: 写实现**

在 `service.py` 顶部（`WorkflowService` 之前）加异常：
```python
class WorkflowNotTerminal(RuntimeError):
    def __init__(self, run_id: str, status: WorkflowStatus) -> None:
        super().__init__(f"run {run_id} 处于 {status.value}，仅终态(success/failed)可 rerun")
        self.run_id = run_id
        self.status = status
```

在 `WorkflowService` 加 `rerun`（置于 `get` 之后），仅允许终态：
```python
    def rerun(self, run_id: str) -> WorkflowRun:
        old = self._store.get(run_id)   # 不存在则抛 WorkflowRunNotFound
        if old.status not in (WorkflowStatus.SUCCESS, WorkflowStatus.FAILED):
            raise WorkflowNotTerminal(run_id, old.status)   # 正在跑/排队中不允许 rerun
        return self.run(old.name, old.params, rerun_of=run_id)
```

- [ ] **Step 4: 跑确认 PASS（2 passed）+ 全量 + ruff + purity**

Run: `.venv/bin/pytest tests/test_workflow_rerun.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 3 passed；全量 PASS；All checks passed；purity 2 passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/service.py tests/test_workflow_rerun.py
git commit -m "feat: add WorkflowService.rerun (new run_id, preserves history)"
```

---

### Task 5: ThreadWorkflowExecutor（后台）+ 最终回归

**Files:**
- Modify: `claw_engine/engine/workflows/executor.py`（加 `ThreadWorkflowExecutor`）
- Test: `tests/test_workflow_thread_executor.py`

- [ ] **Step 1: 写失败测试（用 event 同步，确定性验证后台执行）**

```python
# tests/test_workflow_thread_executor.py
import threading
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService
from claw_engine.engine.workflows.executor import ThreadWorkflowExecutor
from claw_engine.engine.workflows.contracts import WorkflowStatus


def test_thread_executor_runs_in_background_then_completes():
    started = threading.Event()
    release = threading.Event()

    def handler(params, ctx):
        started.set()
        assert release.wait(timeout=5)
        ctx.report_progress("done")
        return {"ok": True}

    reg = EngineRegistry(); reg.register_workflow("bg", handler)
    store = MemoryWorkflowStore()
    ex = ThreadWorkflowExecutor()
    try:
        svc = WorkflowService(reg, store, executor=ex)
        run = svc.run("bg", {})
        assert started.wait(timeout=5)                       # handler 已在后台启动
        # 尚未释放 → 非终态（证明 run() 没有阻塞到完成）
        assert svc.get(run.run_id).status in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING)
        release.set()
        ex.wait_all(timeout=5)
        final = svc.get(run.run_id)
        assert final.status is WorkflowStatus.SUCCESS
        assert final.result == {"ok": True}
        assert [p.message for p in final.progress] == ["done"]
    finally:
        ex.shutdown()

def test_submit_after_shutdown_raises():
    import pytest
    ex = ThreadWorkflowExecutor()
    ex.shutdown()
    with pytest.raises(RuntimeError):
        ex.submit(lambda: None)
```

- [ ] **Step 2: 跑确认 FAIL（ImportError: ThreadWorkflowExecutor）**

Run: `.venv/bin/pytest tests/test_workflow_thread_executor.py -q`
Expected: FAIL

- [ ] **Step 3: 写实现（追加到 `executor.py`）**

```python
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Optional


class ThreadWorkflowExecutor:
    """后台线程池执行；测试可用 wait_all 等待。"""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: List[Future] = []
        self._shutdown = False

    def submit(self, fn: Callable[[], None]) -> None:
        if self._shutdown:
            raise RuntimeError("executor 已 shutdown，无法再提交任务")
        self._futures.append(self._pool.submit(fn))

    def wait_all(self, timeout: Optional[float] = None) -> None:
        for fut in list(self._futures):
            fut.result(timeout=timeout)   # 传播 handler 调度层异常（业务异常已在 _execute 内吞）

    def shutdown(self) -> None:
        self._shutdown = True
        self._pool.shutdown(wait=True)
```
（`Callable` 已在文件顶部 import；确保 `from typing import Callable, List, Optional` 齐全。）

- [ ] **Step 4: 跑确认 PASS + 最终全量 + ruff + purity（P4 验收）**

Run: `.venv/bin/pytest tests/test_workflow_thread_executor.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 2 passed；全量 PASS；All checks passed；purity 2 passed（engine/workflows 全程无 CLI/业务字面量）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/workflows/executor.py tests/test_workflow_thread_executor.py
git commit -m "feat: add ThreadWorkflowExecutor for background workflow runs"
```

---

## Self-Review

**1. 决策覆盖：**
- 决策1（P4 只做 workflow，skill→P5）→ 全程不含 skill；边界已声明 ✅
- 决策2（workflow=注册 handler，引擎拥有生命周期）→ Task 1 `WorkflowRun` + Task 2 registry + Task 3 service ✅
- 决策3（执行器抽象 + InlineExecutor 默认）→ Task 1 `WorkflowExecutor` Protocol + Task 2 `InlineExecutor` + Task 5 `ThreadWorkflowExecutor` ✅
- 决策4（rerun 新 run_id 保留历史）→ Task 4 `test_rerun_creates_new_run_preserving_history` ✅
- 进度事件 → Task 1 `ProgressEvent`/`ctx.report_progress` + Task 3 进度捕获测试 ✅
- 失败归一（handler 抛错→FAILED，不冒泡，进度保留）→ Task 3 `test_run_failure_records_error_and_keeps_progress` ✅
- 任务状态机（PENDING→RUNNING→SUCCESS/FAILED）→ Task 3 + Task 5 后台非终态断言 ✅
- 终态字段互斥（with_result 清 error / with_error 清 result）→ Task 1 `test_terminal_fields_are_mutually_exclusive` ✅
- 进度 fraction 范围校验（越界→ValueError→FAILED）→ Task 3 `test_invalid_fraction_fails_run` ✅
- rerun 仅终态 run（RUNNING/PENDING 抛 `WorkflowNotTerminal`）→ Task 4 `test_rerun_non_terminal_run_raises` ✅
- ThreadExecutor shutdown 后 submit 抛 RuntimeError → Task 5 `test_submit_after_shutdown_raises` ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码。✅

**3. 类型/签名一致性：** `WorkflowRun`(run_id/name/params/status/progress/result/error/rerun_of/created_at/updated_at) + `with_status/with_progress/with_result/with_error`；`ProgressEvent`(message/step/fraction/ts)；`WorkflowStore`(save/get)；`WorkflowExecutor`(submit)；`WorkflowContext.report_progress(message,step,fraction)`；`WorkflowHandler=(params,ctx)->result`；`WorkflowService(registry,store,executor).run/get/rerun`；`EngineRegistry.register_workflow/resolve_workflow` + `WorkflowNotRegistered`；`MemoryWorkflowStore`+`WorkflowRunNotFound`；`InlineExecutor`/`ThreadWorkflowExecutor.submit/wait_all/shutdown`。各 task 间一致。✅

**已知取舍（实现注意）：**
- handler 失败用 `except Exception`（业务失败归一为 FAILED）——与 backend 的窄异常策略相反，且已在设计基线说明理由。
- WorkflowStore V1 仅 memory（Protocol 已留口，sqlite 同 SessionStore 模式后续可加）。
- run_workflow 作为「agent 可调用 tool」的暴露延后（需 MCP/tool-bridge，属 channel/集成）；P4 仅引擎 API。
- ThreadExecutor 仅本机线程池；并发去重/分布式不在 P4。
- `WorkflowContext.report_progress` 走 store get+save，非原子；单 run 内串行调用安全，跨线程并发更新同一 run 不在 P4 范围（一个 run 由单一 handler 线程驱动）。
