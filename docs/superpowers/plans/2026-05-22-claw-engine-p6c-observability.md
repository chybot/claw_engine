# claw_engine P6c — Observability / Tracer seam（强制脱敏）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 加一个 span 式 `Tracer` seam（NoOp 默认 / Memory 测试 / Langfuse 留 adapter），在 `Engine.run_turn` 给每个 agent turn 打 trace（成功+失败都打），带全维度 `{workspace_id, user_id, session_id, backend_name}`，并在引擎边界强制脱敏——Tracer 永远拿不到明文 secret。

**Architecture:** `engine/observability/`（`Tracer`/`TraceSpan` Protocol + `TraceDims` + `NoOpTracer` + `MemoryTracer`）。`Engine` 注入 `tracer`（默认 NoOp），`run_turn` 开 span、记 tool 事件、finish(ok/error)。`user_id` + 已脱敏 env 经 ConversationService/WorkspaceConversationGateway 穿到 run_turn metadata（加法式，向后兼容）。真实 `LangfuseTracer` 在 adapters 实现同 Protocol（P6c 不接 SDK，仅证明 seam）。

**Tech Stack:** Python 3.11+，标准库，pytest，ruff。全 hermetic（NoOp/Memory tracer，不接 langfuse SDK）。

**前置参考：** spec §6.1（langfuse 带 workspace 维度）、§6.3（脱敏）、P5 `ResolvedWorkspace.redacted_env()`、P1 `Engine.run_turn`、P3 `ConversationService`、P5 `WorkspaceConversationGateway`。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策
1. **Tracer span 式**：`tracer.start_trace(name, dims, input, metadata) -> TraceSpan`（context manager）；`span.record_tool(name, input, output)`；`span.finish(output, error)`。`NoOpTracer` 默认、`MemoryTracer` 测试、`LangfuseTracer` 留 adapter。
2. **redaction 强制点 = 引擎边界**。**关键不变式：`run_turn` 把真实 `env` 与 backend `metadata` 都不交给 Tracer；Tracer 只见 prompt + `dims`（4 个安全字段）+ 独立的 `trace_metadata`（调用方只放已 `redacted_env()` 脱敏的安全字段）**。即 `run_turn(metadata=…给backend/dims, trace_metadata=…给tracer)` 二者分离——杜绝公开入口把含 secret 的 metadata 原样喂给 tracer。加 e2e 断言强制。
3. **失败也打 trace**：`run_turn` 成功 → `span.finish(output=final_text)`；`ERROR` → `span.finish(error=...)` 后再抛 `AgentRunFailed`。引擎前的拒绝（InboundAuthError/WorkspaceAccessDenied/UnknownUser/WorkspaceRouteError）**不打 trace**（属安全事件，后续单独 metrics）。
4. **全维度**：`TraceDims{workspace_id, user_id, session_id, backend_name}`。user_id 经 gateway→ConversationService→run_turn metadata 穿透（修复 algo-bot 缺 workspace 维度的问题）。

### 关键不变式与取舍
- **P6c 强保证**：engine 提供的 `env` / backend `metadata` / `dims` 不会把 secret 泄露给 Tracer（env 只进 backend；backend metadata 不进 tracer；tracer 侧 env 一律 `redacted_env()`）。
- **P6c 不保证**：agent 生成内容、`record_tool` 的 tool input/output、`final_text` 里若回显 secret——不做深度 scrub（属 agent 自身/后续责任）。故契约表述**收窄**为「engine-provided env/metadata/dims 不泄露」，而非「整条 trace 永不含 secret」。
- enforcement = `run_turn` **既不把 `env`、也不把 backend `metadata` 传给 tracer**（只传 dims + trace_metadata）+ e2e 断言。
- **向后兼容**：`Engine(registry, tracer=None)` 默认 NoOp；`ConversationService.handle(..., user_id=None, trace_metadata=None)`；`run_turn(..., metadata=None)` 既有调用不变。P1–P6b 测试全绿。
- LangfuseTracer adapter（实现 Tracer Protocol、映射到 langfuse SDK，结构参考 algo-bot `langfuse_tracing.py`）= 部署步骤，P6c 不实现（不引入 SDK 依赖）；seam 用 NoOp/Memory 证明。

### trace 流（run_turn 内）
```
dims = TraceDims(workspace_id, user_id, session_id from metadata; backend_name from param)
# metadata(给 backend/dims) 与 trace_metadata(给 tracer) 分离；tracer 不见 backend metadata/env
with tracer.start_trace("agent-turn", dims=dims, input=prompt, metadata=trace_metadata) as span:
    for event in backend.run(req):
        TOOL_CALL_COMPLETED -> span.record_tool(name, input, output)
        ERROR -> span.finish(error=...); raise AgentRunFailed
        TURN_COMPLETED -> result=...; span.finish(output=final_text)
    (无终态 -> span.finish(error); raise RuntimeError)
return result
```

---

### Task 1: Tracer 契约 + NoOpTracer

**Files:**
- Create: `claw_engine/engine/observability/__init__.py`
- Create: `claw_engine/engine/observability/contracts.py`
- Create: `claw_engine/engine/observability/noop.py`
- Test: `tests/test_tracer_contracts.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tracer_contracts.py
import dataclasses
import pytest
from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.engine.observability.noop import NoOpTracer

def test_trace_dims_frozen_and_defaults():
    d = TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="codex")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.user_id = "x"
    assert TraceDims().workspace_id is None

def test_noop_tracer_span_is_context_manager_and_noop():
    tracer = NoOpTracer()
    span = tracer.start_trace("agent-turn", dims=TraceDims(), input="hi", metadata={})
    with span as s:
        s.record_tool("shell", input={"cmd": "ls"}, output="out")
        s.finish(output="done")          # 不抛即可
    # 再次使用也不报错（幂等 no-op）
    span.finish(error="x")
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_tracer_contracts.py -q`
Expected: FAIL（ModuleNotFoundError: ...observability.contracts）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/observability/contracts.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class TraceDims:
    workspace_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    backend_name: Optional[str] = None


@runtime_checkable
class TraceSpan(Protocol):
    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None: ...
    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None: ...
    def __enter__(self) -> "TraceSpan": ...
    def __exit__(self, exc_type, exc, tb) -> bool: ...


@runtime_checkable
class Tracer(Protocol):
    def start_trace(self, name: str, *, dims: TraceDims, input: Any = None,
                    metadata: Optional[Mapping[str, Any]] = None) -> TraceSpan: ...
```

```python
# claw_engine/engine/observability/noop.py
from __future__ import annotations
from typing import Any, Mapping, Optional
from claw_engine.engine.observability.contracts import TraceDims


class NoOpSpan:
    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None:
        return None

    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None:
        return None

    def __enter__(self) -> "NoOpSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class NoOpTracer:
    """默认 tracer：完全不记录。"""

    def start_trace(self, name: str, *, dims: TraceDims, input: Any = None,
                    metadata: Optional[Mapping[str, Any]] = None) -> NoOpSpan:
        return NoOpSpan()
```

- [ ] **Step 4: PASS（2 passed）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_tracer_contracts.py -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 2 passed；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/observability/__init__.py claw_engine/engine/observability/contracts.py claw_engine/engine/observability/noop.py tests/test_tracer_contracts.py
git commit -m "feat: add Tracer/TraceSpan seam + TraceDims + NoOpTracer"
```

---

### Task 2: MemoryTracer（测试用，记录 trace）

**Files:**
- Create: `claw_engine/engine/observability/memory.py`
- Test: `tests/test_memory_tracer.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_tracer.py
from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.engine.observability.memory import MemoryTracer

def test_records_dims_input_tools_and_output():
    tracer = MemoryTracer()
    dims = TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="codex")
    with tracer.start_trace("agent-turn", dims=dims, input="hi", metadata={"k": "v"}) as span:
        span.record_tool("shell", input={"cmd": "ls"}, output="out")
        span.finish(output="done")
    assert len(tracer.traces) == 1
    rec = tracer.traces[0]
    assert rec.name == "agent-turn" and rec.dims == dims
    assert rec.input == "hi" and rec.metadata == {"k": "v"}
    assert rec.tools == [{"name": "shell", "input": {"cmd": "ls"}, "output": "out"}]
    assert rec.output == "done" and rec.error is None and rec.finished is True

def test_records_error():
    tracer = MemoryTracer()
    with tracer.start_trace("agent-turn", dims=TraceDims(), input="x") as span:
        span.finish(error="boom")
    assert tracer.traces[0].error == "boom" and tracer.traces[0].output is None

def test_exit_without_finish_auto_finishes():
    tracer = MemoryTracer()
    with tracer.start_trace("agent-turn", dims=TraceDims(), input="x"):
        pass                                  # 未显式 finish
    assert tracer.traces[0].finished is True   # __exit__ 兜底 finish

def test_finish_is_idempotent():
    tracer = MemoryTracer()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    span.finish(output="first")
    span.finish(output="second")              # 第二次忽略
    assert tracer.traces[0].output == "first"
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_memory_tracer.py -q`
Expected: FAIL（ModuleNotFoundError: ...observability.memory）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/observability/memory.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
from claw_engine.engine.observability.contracts import TraceDims


@dataclass
class TraceRecord:
    name: str
    dims: TraceDims
    input: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    output: Any = None
    error: Optional[str] = None
    finished: bool = False


class MemorySpan:
    def __init__(self, record: TraceRecord) -> None:
        self._record = record

    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None:
        self._record.tools.append({"name": name, "input": input, "output": output})

    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None:
        if self._record.finished:
            return
        self._record.output = output
        self._record.error = error
        self._record.finished = True

    def __enter__(self) -> "MemorySpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._record.finished:
            self.finish(error=None if exc is None else str(exc))
        return False


class MemoryTracer:
    """测试/开发用：把 trace 记录到内存，供断言。"""

    def __init__(self) -> None:
        self.traces: List[TraceRecord] = []

    def start_trace(self, name: str, *, dims: TraceDims, input: Any = None,
                    metadata: Optional[Mapping[str, Any]] = None) -> MemorySpan:
        record = TraceRecord(name=name, dims=dims, input=input, metadata=dict(metadata or {}))
        self.traces.append(record)
        return MemorySpan(record)
```

- [ ] **Step 4: PASS（4 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_memory_tracer.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 4 passed；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/observability/memory.py tests/test_memory_tracer.py
git commit -m "feat: add MemoryTracer + TraceRecord for hermetic trace assertions"
```

---

### Task 3: Engine.run_turn 打 trace（成功+失败）

**Files:**
- Modify: `claw_engine/engine/orchestration/engine.py`
- Test: `tests/test_engine_tracing.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_engine_tracing.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentErrorKind, AgentRunResult, ToolEvent,
    TokenUsage, BackendCapabilities, BackendHealth,
)


class _ToolThenDoneBackend:
    name = "b"
    def capabilities(self):
        return BackendCapabilities(True, False, True, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        yield AgentEvent(kind=AgentEventKind.TOOL_CALL_COMPLETED,
                         tool=ToolEvent(name="shell", input={"cmd": "ls"}, output="out"))
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text="answer")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt",
                         result=AgentRunResult(backend_thread_id="bt", final_text="answer", usage=TokenUsage()))


class _ErrorBackend:
    name = "b"
    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        yield AgentEvent(kind=AgentEventKind.ERROR,
                         error=AgentError(kind=AgentErrorKind.BACKEND_CRASH, message="boom"))


def _engine(backend, tracer):
    reg = EngineRegistry()
    reg.register_backend("b", lambda: backend)
    return Engine(reg, tracer=tracer)

def test_success_turn_traced_with_dims_tools_output():
    tracer = MemoryTracer()
    eng = _engine(_ToolThenDoneBackend(), tracer)
    res = eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp", env={},
                       metadata={"workspace_id": "ws1", "user_id": "u1", "session_id": "s1"})
    assert res.final_text == "answer"
    assert len(tracer.traces) == 1
    rec = tracer.traces[0]
    assert rec.dims == TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="b")
    assert rec.input == "hi"
    assert rec.tools == [{"name": "shell", "input": {"cmd": "ls"}, "output": "out"}]
    assert rec.output == "answer" and rec.error is None

def test_failed_turn_traced_with_error_then_raises():
    tracer = MemoryTracer()
    eng = _engine(_ErrorBackend(), tracer)
    with pytest.raises(AgentRunFailed):
        eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp", env={}, metadata={})
    assert len(tracer.traces) == 1
    assert "boom" in tracer.traces[0].error and tracer.traces[0].output is None

def test_default_engine_uses_noop_tracer():
    reg = EngineRegistry()
    reg.register_backend("b", lambda: _ToolThenDoneBackend())
    eng = Engine(reg)                         # 不传 tracer -> NoOp，不报错
    assert eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp", env={}).final_text == "answer"

def test_engine_does_not_leak_env_or_backend_metadata_to_tracer():
    # 公开入口级别防泄露：env 与 backend metadata 里的 secret 都不应进 tracer
    tracer = MemoryTracer()
    eng = _engine(_ToolThenDoneBackend(), tracer)
    eng.run_turn(backend_name="b", prompt="hi", cwd="/tmp",
                 env={"TOKEN": "super-secret"},
                 metadata={"workspace_id": "ws1", "leak": "super-secret"})  # 非 dim 字段含 secret
    rec = tracer.traces[0]
    assert "super-secret" not in repr(rec)        # env 不入 tracer；backend metadata 也不入
    assert rec.dims.workspace_id == "ws1"         # 仅 4 个安全 dim 字段进 tracer
    assert rec.metadata == {}                     # 未传 trace_metadata -> tracer metadata 为空
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_engine_tracing.py -q`
Expected: FAIL（Engine 不接受 tracer / 不打 trace）

- [ ] **Step 3: 改实现（`engine.py`）**

`Engine.__init__` 加 `tracer`：
```python
from claw_engine.engine.observability.contracts import TraceDims, Tracer
from claw_engine.engine.observability.noop import NoOpTracer

class Engine:
    def __init__(self, registry: EngineRegistry, tracer: Optional[Tracer] = None) -> None:
        self._registry = registry
        self._tracer = tracer or NoOpTracer()
```
`run_turn` 加 `trace_metadata` 参数并包 trace（保留能力 gate、ERROR→AgentRunFailed、缺终态→RuntimeError 语义）：
```python
    def run_turn(
        self,
        *,
        backend_name: str,
        prompt: str,
        cwd: str,
        env: Mapping[str, str],
        backend_thread_id: Optional[str] = None,
        model: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,        # 给 backend / 取 dims
        trace_metadata: Optional[Mapping[str, Any]] = None,  # 给 tracer（调用方负责脱敏）
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> AgentRunResult:
        backend = self._registry.resolve_backend(backend_name)
        caps = backend.capabilities()
        md = metadata or {}
        req = AgentRunRequest(
            prompt=prompt, cwd=cwd, env=env,
            backend_thread_id=backend_thread_id if caps.supports_resume else None,
            model=model, metadata=md,
        )
        dims = TraceDims(workspace_id=md.get("workspace_id"), user_id=md.get("user_id"),
                         session_id=md.get("session_id"), backend_name=backend_name)
        result: Optional[AgentRunResult] = None
        # 关键：env 与 backend metadata 都不交给 tracer；tracer 只见 prompt / dims / trace_metadata
        with self._tracer.start_trace("agent-turn", dims=dims, input=prompt,
                                      metadata=trace_metadata) as span:
            for event in backend.run(req):
                if event.kind is AgentEventKind.MESSAGE_DELTA and not caps.supports_streaming:
                    continue
                if on_event is not None:
                    on_event(event)
                if event.kind is AgentEventKind.TOOL_CALL_COMPLETED and event.tool is not None:
                    span.record_tool(event.tool.name, input=event.tool.input, output=event.tool.output)
                if event.kind is AgentEventKind.ERROR:
                    assert event.error is not None
                    span.finish(error=f"{event.error.kind.value}: {event.error.message}")
                    raise AgentRunFailed(event.error)
                if event.kind is AgentEventKind.TURN_COMPLETED:
                    assert event.result is not None
                    result = event.result
            if result is None:
                span.finish(error="backend 未产出终态 TURN_COMPLETED/ERROR")
                raise RuntimeError("backend 未产出终态 TURN_COMPLETED/ERROR，违反契约")
            span.finish(output=result.final_text)
        return result
```
（确保 `Any`、`Callable`、`Optional`、`Mapping` 已 import。）

- [ ] **Step 4: PASS（3 passed）+ 全量（run_turn 既有测试默认 NoOp 不回归）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_engine_tracing.py tests/test_engine_run_turn.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: tracing 4 passed + run_turn 既有 3 passed；全量 PASS；purity 2 passed（observability 在 engine、无业务/CLI 字面量）；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/orchestration/engine.py tests/test_engine_tracing.py
git commit -m "feat: trace agent turns in Engine.run_turn (success + failure, full dims)"
```

---

### Task 4: 穿 user_id + redacted env 进 trace + 脱敏 e2e + 最终回归

**Files:**
- Modify: `claw_engine/engine/orchestration/conversation.py`（handle 加 user_id/trace_metadata → metadata）
- Modify: `claw_engine/engine/orchestration/workspace_gateway.py`（穿 user_id + redacted_env → trace_metadata）
- Test: `tests/test_trace_redaction_e2e.py`

- [ ] **Step 1: 写失败测试（全链路：gateway→trace；断言无 secret 明文 + 全维度）**

```python
# tests/test_trace_redaction_e2e.py
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.observability.memory import MemoryTracer
from tests.contract.fake_backend import FakeBackend

def test_engine_trace_metadata_uses_redacted_env_and_never_receives_plain_env_secret():
    tracer = MemoryTracer()
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    convo = ConversationService(Engine(reg, tracer=tracer), MemorySessionStore())
    config = LayeredConfigProvider(global_env={"REGION": "id"})
    secrets = InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "super-secret"}})
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws")
    gw = WorkspaceConversationGateway(resolver, convo)

    gw.handle(workspace_id="ws1", channel="webhook", external_thread_key="t",
              text="hello", backend_name="fake", message_id="m1", user_id="u1")

    assert len(tracer.traces) == 1
    rec = tracer.traces[0]
    # 全维度
    assert rec.dims.workspace_id == "ws1" and rec.dims.user_id == "u1"
    assert rec.dims.session_id is not None and rec.dims.backend_name == "fake"
    # engine-provided 强保证：trace metadata 里 env 是脱敏视图，dims/metadata 无明文 secret
    assert rec.metadata["redacted_env"]["JIRA_TOKEN"] == "***"
    assert "super-secret" not in repr(rec.dims)
    assert "super-secret" not in repr(rec.metadata)
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_trace_redaction_e2e.py -q`
Expected: FAIL（gateway/convo 还没穿 user_id + redacted_env）

- [ ] **Step 3: 改实现**

a) `conversation.py` `ConversationService.handle` 加 `user_id`/`trace_metadata`：
```python
    def handle(self, *, workspace_id: str, channel: str, external_thread_key: str,
               text: str, cwd: str, env: Mapping[str, str], backend_name: str,
               max_rounds: int = DEFAULT_MAX_ROUNDS, message_id: Optional[str] = None,
               model: Optional[str] = None, user_id: Optional[str] = None,
               trace_metadata: Optional[Mapping[str, Any]] = None) -> AgentRunResult:
        ... 既有 session/dedup/rounds 逻辑不变 ...
        metadata = {"workspace_id": workspace_id, "session_id": session.session_id,
                    "channel": channel, "user_id": user_id}   # 给 backend / 取 dims（非 secret）
        result = self._engine.run_turn(
            backend_name=session.backend_name, prompt=text, cwd=cwd, env=env,
            backend_thread_id=session.backend_thread_id, model=model,
            metadata=metadata, trace_metadata=trace_metadata,  # 二者分离：trace_metadata 给 tracer
        )
        ... 既有 save/mark_processed 不变 ...
```
（import `Any`/`Mapping`。）

b) `workspace_gateway.py` `handle` 穿 user_id + redacted_env：
```python
        ws = self._resolver.resolve(workspace_id, user_id)
        return self._conversation.handle(
            workspace_id=workspace_id, channel=channel, external_thread_key=external_thread_key,
            text=text, cwd=ws.cwd, env=ws.env,
            backend_name=ws.backend_name or backend_name,
            max_rounds=ws.max_rounds or DEFAULT_MAX_ROUNDS,
            message_id=message_id, model=model,
            user_id=user_id,
            trace_metadata={"redacted_env": ws.redacted_env()},   # 脱敏 env 进 trace metadata
        )
```

- [ ] **Step 4: PASS + 最终全量 + ruff + purity（P6c 验收）**

Run: `.venv/bin/pytest tests/test_trace_redaction_e2e.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: PASS；全量 PASS（P3 conversation、P5 gateway 等不回归）；All checks passed；purity 2 passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/orchestration/conversation.py claw_engine/engine/orchestration/workspace_gateway.py tests/test_trace_redaction_e2e.py
git commit -m "feat: thread user_id + redacted env into trace metadata; enforce no-secret-in-trace"
```

---

## Self-Review

**1. 决策覆盖：**
- 决策1（span 式 Tracer）→ Task 1 `Tracer/TraceSpan` + NoOp，Task 2 Memory ✅
- 决策2（引擎边界先 redact，Tracer 不见明文）→ Task 3 run_turn **分离 metadata/trace_metadata**（env 与 backend metadata 都不给 tracer）+ `test_engine_does_not_leak_env_or_backend_metadata_to_tracer`（公开入口防泄露）+ Task 4 `redacted_env` 进 trace_metadata + `test_engine_trace_metadata_uses_redacted_env_and_never_receives_plain_env_secret` ✅
- 决策3（成败都打、引擎前拒绝不打）→ Task 3 `test_failed_turn_traced_with_error_then_raises`；引擎前拒绝在路由层抛异常、不经 run_turn 故无 trace ✅
- 决策4（全维度）→ Task 3 dims 断言 + Task 4 e2e 全维度断言（user_id 经 gateway→convo→metadata 穿透）✅
- 向后兼容（Engine tracer 默认 NoOp；handle 加法式）→ Task 3 `test_default_engine_uses_noop_tracer` + 既有测试不回归 ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码（测试片段避免 `a; b` 复合语句以过 ruff E702）。✅

**3. 类型/签名一致性：** `TraceDims{workspace_id,user_id,session_id,backend_name}`；`Tracer.start_trace(name,*,dims,input,metadata)->TraceSpan`；`TraceSpan.record_tool(name,*,input,output)`/`finish(*,output,error)`/`__enter__`/`__exit__`；`NoOpTracer`/`MemoryTracer`/`TraceRecord`；`Engine(registry, tracer=None)` + run_turn 内 trace；`ConversationService.handle(...,user_id=None,trace_metadata=None)`；`WorkspaceConversationGateway.handle(...,user_id)` 穿 trace_metadata。各 task 间一致。✅

**已知取舍（实现注意）：**
- secret 仅经 backend env，绝不入 trace；trace 侧 env 一律 `redacted_env()`。enforcement = run_turn 不把 `env` 给 tracer + e2e 断言无明文。
- tool 载荷（record_tool 的 input/output）原样记录，不做深度 scrub（P6c 只保证 env/metadata 脱敏）。已知限制。
- 引擎前拒绝（auth/route/未知用户）不打 trace（属安全事件，后续单独 metrics/log）。
- 真实 `LangfuseTracer` adapter（实现 Tracer Protocol、映射 langfuse SDK，session_id/user_id/workspace_id/backend_name 作 trace 一等属性）= 部署步骤，P6c 不引入 SDK；seam 已用 NoOp/Memory 证明。
