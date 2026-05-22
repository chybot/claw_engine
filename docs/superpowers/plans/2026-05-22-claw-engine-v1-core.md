# claw_engine V1 Core — Backend Contract + Walking Skeleton 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 跑通「prompt → 经 EngineRegistry 解析到的 CodeAgentBackend → 归一 AgentEvent 流 → AgentRunResult」的最小闭环，并用 CI 纯净度测试钉死 engine/adapters 边界。

**Architecture:** 严格按 spec §3（L2 Backend Contract）与 §4（engine/adapters 边界）。engine 只认归一类型，backend 实现住在 `adapters/backends/`。用注入式 `spawn` 让 CodexCliBackend 可被 hermetic 测试；用 `FakeBackend` 跑契约测试与端到端 smoke，不依赖真实 CLI。

**Tech Stack:** Python 3.11+，pytest，ruff，标准库 `subprocess`/`ast`。无第三方运行时依赖（V1 核心）。

参考 spec：`docs/superpowers/specs/2026-05-22-codeagent-engine-design.md`

---

### Task 0: 项目脚手架与工具链

**Files:**
- Create: `pyproject.toml`
- Create: `claw_engine/__init__.py`
- Create: `claw_engine/engine/__init__.py`, `claw_engine/engine/runtime/__init__.py`
- Create: `claw_engine/adapters/__init__.py`, `claw_engine/adapters/backends/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: 写 `pyproject.toml`**

```toml
[project]
name = "claw_engine"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["claw_engine*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 2: 建包目录与空 `__init__.py`**

Run:
```bash
mkdir -p claw_engine/engine/runtime claw_engine/adapters/backends tests/contract tests/purity
touch claw_engine/__init__.py claw_engine/engine/__init__.py claw_engine/engine/runtime/__init__.py \
      claw_engine/adapters/__init__.py claw_engine/adapters/backends/__init__.py tests/__init__.py
```

- [ ] **Step 3: 安装并验证**

Run: `pip install -e ".[dev]" && pytest -q`
Expected: `no tests ran`（脚手架就绪，无报错）

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml claw_engine tests
git commit -m "chore: scaffold claw_engine package and tooling"
```

---

### Task 1: L2 归一类型与 Backend 契约

**Files:**
- Create: `claw_engine/engine/runtime/contracts.py`
- Test: `tests/test_contracts.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contracts.py
import dataclasses
import pytest
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest, AgentRunResult,
    AgentError, AgentErrorKind, BackendCapabilities, ToolEvent, TokenUsage,
)

def test_request_is_frozen():
    req = AgentRunRequest(prompt="hi", cwd="/tmp", env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.prompt = "x"

def test_request_defaults():
    req = AgentRunRequest(prompt="hi", cwd="/tmp", env={})
    assert req.backend_thread_id is None
    assert req.timeout_s == 1800

def test_turn_completed_carries_result():
    res = AgentRunResult(backend_thread_id="t1", final_text="done", usage=TokenUsage())
    ev = AgentEvent(kind=AgentEventKind.TURN_COMPLETED, result=res)
    assert ev.kind is AgentEventKind.TURN_COMPLETED
    assert ev.result.final_text == "done"
    assert ev.result.status == "ok"

def test_error_event_carries_error():
    err = AgentError(kind=AgentErrorKind.TIMEOUT, message="slow", retriable=True)
    ev = AgentEvent(kind=AgentEventKind.ERROR, error=err)
    assert ev.error.kind is AgentErrorKind.TIMEOUT

def test_capabilities_fields():
    caps = BackendCapabilities(
        supports_resume=True, supports_streaming=False,
        supports_tools=True, supports_mcp=False, auth_modes=("api_key",),
    )
    assert caps.supports_resume and not caps.supports_streaming
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_contracts.py -q`
Expected: FAIL — `ModuleNotFoundError: claw_engine.engine.runtime.contracts`

- [ ] **Step 3: 写实现（完整对齐 spec §3.1/§3.2）**

```python
# claw_engine/engine/runtime/contracts.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentRunRequest:
    prompt: str
    cwd: str
    env: Mapping[str, str]
    backend_thread_id: Optional[str] = None   # 后端 resume 句柄；None=新会话/首轮。对引擎不透明
    model: Optional[str] = None
    timeout_s: int = 1800
    attachments: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AgentEventKind(str, Enum):
    THREAD_STARTED = "thread_started"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_COMPLETED = "message_completed"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TURN_COMPLETED = "turn_completed"   # 成功终态，携带 result（失败一律走 ERROR）
    ERROR = "error"                     # 失败终态，携带 error


class AgentErrorKind(str, Enum):
    AUTH = "auth"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    BACKEND_CRASH = "backend_crash"
    PROTOCOL = "protocol"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ToolEvent:
    name: str
    input: Optional[Any] = None
    output: Optional[Any] = None
    skill: Optional[str] = None


@dataclass(frozen=True)
class AgentError:
    kind: AgentErrorKind
    message: str
    retriable: bool = False
    raw: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class AgentRunResult:
    backend_thread_id: Optional[str]
    final_text: str
    usage: TokenUsage
    status: str = "ok"   # 恒为 "ok"：TURN_COMPLETED 只表成功


@dataclass(frozen=True)
class AgentEvent:
    kind: AgentEventKind
    backend_thread_id: Optional[str] = None
    text: Optional[str] = None
    tool: Optional[ToolEvent] = None
    usage: Optional[TokenUsage] = None
    result: Optional[AgentRunResult] = None
    error: Optional[AgentError] = None
    ts: float = 0.0
    raw: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class BackendCapabilities:
    supports_resume: bool
    supports_streaming: bool
    supports_tools: bool
    supports_mcp: bool
    auth_modes: tuple[str, ...]


@dataclass(frozen=True)
class BackendHealth:
    ok: bool
    detail: str = ""


@runtime_checkable
class CodeAgentBackend(Protocol):
    name: str
    def capabilities(self) -> BackendCapabilities: ...
    def run(self, request: AgentRunRequest) -> Iterator[AgentEvent]: ...
    def healthcheck(self) -> BackendHealth: ...
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_contracts.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/runtime/contracts.py tests/test_contracts.py
git commit -m "feat: add L2 agent event + backend contract types"
```

---

### Task 2: 可复用的 Backend 契约测试套件 + FakeBackend

> 任何 backend（FakeBackend / CodexCliBackend / 未来 claude）都必须通过这套用例。spec §3.3 的硬规则在此变成可执行断言。

**Files:**
- Create: `claw_engine/engine/runtime/contract_suite.py`（被测试复用的 helper）
- Create: `tests/contract/fake_backend.py`
- Create: `tests/contract/test_fake_backend_contract.py`

- [ ] **Step 1: 写契约校验 helper（先写测试驱动它）**

```python
# tests/contract/test_fake_backend_contract.py
from claw_engine.engine.runtime.contract_suite import assert_valid_event_stream
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind
from tests.contract.fake_backend import FakeBackend

def test_success_stream_is_contract_valid():
    backend = FakeBackend(script="ok")
    req = AgentRunRequest(prompt="hello", cwd="/tmp", env={})
    events = list(backend.run(req))
    assert_valid_event_stream(events)  # 不抛即合规
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "echo: hello"

def test_error_stream_is_contract_valid():
    backend = FakeBackend(script="error")
    req = AgentRunRequest(prompt="x", cwd="/tmp", env={})
    events = list(backend.run(req))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR

def test_double_terminal_is_rejected():
    import pytest
    from claw_engine.engine.runtime.contracts import AgentEvent, AgentEventKind, AgentRunResult, TokenUsage
    bad = [
        AgentEvent(kind=AgentEventKind.TURN_COMPLETED,
                   result=AgentRunResult(backend_thread_id=None, final_text="a", usage=TokenUsage())),
        AgentEvent(kind=AgentEventKind.TURN_COMPLETED,
                   result=AgentRunResult(backend_thread_id=None, final_text="b", usage=TokenUsage())),
    ]
    with pytest.raises(AssertionError):
        assert_valid_event_stream(bad)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/contract -q`
Expected: FAIL — `ModuleNotFoundError: ...contract_suite` 与 `tests.contract.fake_backend`

- [ ] **Step 3: 写契约 helper**

```python
# claw_engine/engine/runtime/contract_suite.py
from __future__ import annotations
from typing import Sequence
from claw_engine.engine.runtime.contracts import AgentEvent, AgentEventKind

_TERMINAL = {AgentEventKind.TURN_COMPLETED, AgentEventKind.ERROR}


def assert_valid_event_stream(events: Sequence[AgentEvent]) -> None:
    """spec §3.3 规则 3：恰好一个终态事件，且必须在末尾；终态字段匹配。"""
    assert events, "事件流不能为空"
    terminals = [e for e in events if e.kind in _TERMINAL]
    assert len(terminals) == 1, f"必须恰好一个终态事件，实际 {len(terminals)}"
    assert events[-1].kind in _TERMINAL, "终态事件必须在末尾"
    last = events[-1]
    if last.kind is AgentEventKind.TURN_COMPLETED:
        assert last.result is not None, "TURN_COMPLETED 必须携带 result"
        assert last.error is None, "TURN_COMPLETED 不得携带 error"
    else:
        assert last.error is not None, "ERROR 必须携带 error"
        assert last.result is None, "ERROR 不得携带 result"
```

- [ ] **Step 4: 写 FakeBackend**

```python
# tests/contract/fake_backend.py
from __future__ import annotations
from typing import Iterator
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest, AgentRunResult, TokenUsage,
    AgentError, AgentErrorKind, BackendCapabilities, BackendHealth, CodeAgentBackend,
)


class FakeBackend(CodeAgentBackend):
    name = "fake"

    def __init__(self, script: str = "ok") -> None:
        self._script = script

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_resume=True, supports_streaming=False,
            supports_tools=True, supports_mcp=False, auth_modes=("api_key",),
        )

    def healthcheck(self) -> BackendHealth:
        return BackendHealth(ok=True)

    def run(self, request: AgentRunRequest) -> Iterator[AgentEvent]:
        tid = request.backend_thread_id or "fake-thread-1"
        yield AgentEvent(kind=AgentEventKind.THREAD_STARTED, backend_thread_id=tid)
        if self._script == "error":
            yield AgentEvent(
                kind=AgentEventKind.ERROR,
                error=AgentError(kind=AgentErrorKind.BACKEND_CRASH, message="boom"),
            )
            return
        text = f"echo: {request.prompt}"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id=tid,
            result=AgentRunResult(backend_thread_id=tid, final_text=text, usage=TokenUsage()),
        )
```

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/contract -q`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add claw_engine/engine/runtime/contract_suite.py tests/contract
git commit -m "feat: add backend contract suite and FakeBackend"
```

---

### Task 3: EngineRegistry（组合根）

**Files:**
- Create: `claw_engine/engine/bootstrap.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_registry.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry, BackendNotRegistered
from tests.contract.fake_backend import FakeBackend

def test_register_and_resolve_backend():
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    backend = reg.resolve_backend("fake")
    assert backend.name == "fake"

def test_unknown_backend_raises():
    reg = EngineRegistry()
    with pytest.raises(BackendNotRegistered):
        reg.resolve_backend("nope")

def test_resolve_is_fresh_instance():
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    assert reg.resolve_backend("fake") is not reg.resolve_backend("fake")
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: claw_engine.engine.bootstrap`

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/bootstrap.py
from __future__ import annotations
from typing import Callable, Dict
from claw_engine.engine.runtime.contracts import CodeAgentBackend


class BackendNotRegistered(KeyError):
    pass


class EngineRegistry:
    """组合根：adapters 在启动时注册实现，engine 只按名解析抽象类型。"""

    def __init__(self) -> None:
        self._backends: Dict[str, Callable[[], CodeAgentBackend]] = {}

    def register_backend(self, name: str, factory: Callable[[], CodeAgentBackend]) -> None:
        self._backends[name] = factory

    def resolve_backend(self, name: str) -> CodeAgentBackend:
        try:
            factory = self._backends[name]
        except KeyError:
            raise BackendNotRegistered(name) from None
        return factory()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_registry.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/bootstrap.py tests/test_registry.py
git commit -m "feat: add EngineRegistry composition root"
```

---

### Task 4: Engine.run_turn 编排（最小）

**Files:**
- Create: `claw_engine/engine/orchestration/__init__.py`
- Create: `claw_engine/engine/orchestration/engine.py`
- Test: `tests/test_engine_run_turn.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_engine_run_turn.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.engine.runtime.contracts import AgentErrorKind
from tests.contract.fake_backend import FakeBackend

def _engine():
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    reg.register_backend("fake_err", lambda: FakeBackend(script="error"))
    return Engine(reg)

def test_run_turn_success_returns_result():
    res = _engine().run_turn(backend_name="fake", prompt="hello", cwd="/tmp", env={})
    assert res.final_text == "echo: hello"
    assert res.backend_thread_id == "fake-thread-1"

def test_run_turn_error_raises_typed():
    with pytest.raises(AgentRunFailed) as ei:
        _engine().run_turn(backend_name="fake_err", prompt="x", cwd="/tmp", env={})
    assert ei.value.error.kind is AgentErrorKind.BACKEND_CRASH

def test_run_turn_collects_events_via_callback():
    seen = []
    res = _engine().run_turn(
        backend_name="fake", prompt="hi", cwd="/tmp", env={}, on_event=seen.append
    )
    kinds = [e.kind.value for e in seen]
    assert kinds == ["thread_started", "message_completed", "turn_completed"]
    assert res.final_text == "echo: hi"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_engine_run_turn.py -q`
Expected: FAIL — `ModuleNotFoundError: ...orchestration.engine`

- [ ] **Step 3: 写实现（消费归一事件，校验契约，gate 能力）**

```python
# claw_engine/engine/orchestration/engine.py
from __future__ import annotations
from typing import Any, Callable, Mapping, Optional
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentRunRequest, AgentRunResult,
)


class AgentRunFailed(RuntimeError):
    def __init__(self, error: AgentError) -> None:
        super().__init__(f"{error.kind.value}: {error.message}")
        self.error = error


class Engine:
    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry

    def run_turn(
        self,
        *,
        backend_name: str,
        prompt: str,
        cwd: str,
        env: Mapping[str, str],
        backend_thread_id: Optional[str] = None,
        model: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> AgentRunResult:
        backend = self._registry.resolve_backend(backend_name)
        caps = backend.capabilities()
        req = AgentRunRequest(
            prompt=prompt, cwd=cwd, env=env,
            backend_thread_id=backend_thread_id if caps.supports_resume else None,
            model=model, metadata=metadata or {},
        )
        result: Optional[AgentRunResult] = None
        for event in backend.run(req):
            # 能力 gate：不支持流式时忽略 delta（spec §3.3 规则 5）
            if event.kind is AgentEventKind.MESSAGE_DELTA and not caps.supports_streaming:
                continue
            if on_event is not None:
                on_event(event)
            if event.kind is AgentEventKind.ERROR:
                assert event.error is not None
                raise AgentRunFailed(event.error)
            if event.kind is AgentEventKind.TURN_COMPLETED:
                assert event.result is not None
                result = event.result
        if result is None:
            raise RuntimeError("backend 未产出终态 TURN_COMPLETED/ERROR，违反契约")
        return result
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_engine_run_turn.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/orchestration tests/test_engine_run_turn.py
git commit -m "feat: add Engine.run_turn orchestration over backend contract"
```

---

### Task 5: CodexCliBackend（adapters，注入式 spawn，hermetic 测试）

**Files:**
- Create: `claw_engine/adapters/backends/codex/__init__.py`
- Create: `claw_engine/adapters/backends/codex/backend.py`
- Test: `tests/test_codex_backend.py`

- [ ] **Step 1: 写失败测试（用 canned codex JSON 行，不起真实进程）**

```python
# tests/test_codex_backend.py
from claw_engine.adapters.backends.codex.backend import CodexCliBackend
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind
from claw_engine.engine.runtime.contract_suite import assert_valid_event_stream

CANNED_OK = [
    '{"type":"thread.started","thread_id":"th_123"}',
    '{"type":"item.completed","item":{"type":"tool_call","name":"shell","input":{"cmd":"ls"}}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}',
]

def _spawn_factory(lines, returncode=0):
    def spawn(argv, cwd, env, timeout_s):
        return iter(lines), (lambda: returncode)
    return spawn

def test_codex_success_normalizes_to_contract():
    backend = CodexCliBackend(spawn=_spawn_factory(CANNED_OK))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[0].kind is AgentEventKind.THREAD_STARTED
    assert events[0].backend_thread_id == "th_123"
    assert any(e.kind is AgentEventKind.TOOL_CALL_COMPLETED and e.tool.name == "shell" for e in events)
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "final answer"
    assert events[-1].result.backend_thread_id == "th_123"

def test_codex_nonzero_exit_maps_to_error():
    backend = CodexCliBackend(spawn=_spawn_factory([], returncode=1))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR

def test_codex_resume_passes_thread_id_in_argv():
    captured = {}
    def spawn(argv, cwd, env, timeout_s):
        captured["argv"] = argv
        return iter(CANNED_OK), (lambda: 0)
    backend = CodexCliBackend(spawn=spawn)
    list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, backend_thread_id="th_9")))
    assert "resume" in captured["argv"]
    assert "th_9" in captured["argv"]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_codex_backend.py -q`
Expected: FAIL — `ModuleNotFoundError: ...adapters.backends.codex.backend`

- [ ] **Step 3: 写实现（CLI 知识封死在此文件，spec §3.3 规则 1）**

```python
# claw_engine/adapters/backends/codex/backend.py
from __future__ import annotations
import json
import shutil
import subprocess
from typing import Callable, Iterator, Mapping, Optional, Tuple
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentErrorKind, AgentRunRequest,
    AgentRunResult, BackendCapabilities, BackendHealth, ToolEvent, TokenUsage,
)

# spawn(argv, cwd, env, timeout_s) -> (stdout 行迭代器, 取退出码的可调用)
SpawnFn = Callable[[list, str, Mapping[str, str], int], Tuple[Iterator[str], Callable[[], int]]]


def _default_spawn(argv, cwd, env, timeout_s):
    proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    def lines() -> Iterator[str]:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait(timeout=timeout_s)
    return lines(), (lambda: proc.returncode if proc.returncode is not None else 0)


class CodexCliBackend:
    name = "codex"

    def __init__(self, command: str = "codex", spawn: Optional[SpawnFn] = None) -> None:
        self._command = command
        self._spawn = spawn or _default_spawn

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_resume=True, supports_streaming=False,
            supports_tools=True, supports_mcp=True, auth_modes=("api_key", "cli_login"),
        )

    def healthcheck(self) -> BackendHealth:
        return BackendHealth(ok=shutil.which(self._command) is not None,
                             detail="" if shutil.which(self._command) else "codex not on PATH")

    def _build_argv(self, req: AgentRunRequest) -> list:
        argv = [self._command, "exec"]
        if req.backend_thread_id:
            argv += ["resume", req.backend_thread_id]
        argv += ["--dangerously-bypass-approvals-and-sandbox", "--json"]
        if req.model:
            argv += ["--model", req.model]
        argv.append(req.prompt)
        return argv

    def run(self, req: AgentRunRequest) -> Iterator[AgentEvent]:
        argv = self._build_argv(req)
        line_iter, returncode = self._spawn(argv, req.cwd, req.env, req.timeout_s)
        thread_id = req.backend_thread_id
        final_text = ""
        for raw_line in line_iter:
            if not raw_line.strip():
                continue
            try:
                evt = json.loads(raw_line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行跳过
            etype = evt.get("type")
            if etype == "thread.started":
                thread_id = evt.get("thread_id") or thread_id
                yield AgentEvent(kind=AgentEventKind.THREAD_STARTED,
                                 backend_thread_id=thread_id, raw=evt)
            elif etype == "item.completed":
                item = evt.get("item") or {}
                itype = item.get("type")
                if itype == "tool_call":
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_CALL_COMPLETED,
                        tool=ToolEvent(name=item.get("name", "tool"),
                                       input=item.get("input"), output=item.get("output")),
                        raw=evt,
                    )
                elif itype == "agent_message":
                    final_text = item.get("text", final_text)
                    yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED,
                                     text=final_text, raw=evt)
        code = returncode()
        if code != 0:
            yield AgentEvent(
                kind=AgentEventKind.ERROR,
                error=AgentError(kind=AgentErrorKind.BACKEND_CRASH,
                                 message=f"codex exited with {code}", retriable=False),
            )
            return
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id=thread_id,
            result=AgentRunResult(backend_thread_id=thread_id, final_text=final_text,
                                  usage=TokenUsage()),
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_codex_backend.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/adapters/backends/codex tests/test_codex_backend.py
git commit -m "feat: add CodexCliBackend adapter with injectable spawn"
```

---

### Task 6: 纯净度测试（CI gate，spec §4.3）

**Files:**
- Create: `tests/purity/test_engine_purity.py`

- [ ] **Step 1: 写测试（一开始应通过；它是回归护栏）**

```python
# tests/purity/test_engine_purity.py
import ast
import pathlib

ENGINE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "claw_engine" / "engine"
FORBIDDEN_TOKENS = ("seatalk", "jira", "codex", "claude")

def _py_files(root):
    return [p for p in root.rglob("*.py")]

def test_engine_has_no_adapter_imports():
    offenders = []
    for path in _py_files(ENGINE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = ",".join(a.name for a in node.names)
            if mod and "adapters" in mod:
                offenders.append(f"{path}: imports {mod}")
    assert not offenders, "engine 不得 import adapters:\n" + "\n".join(offenders)

def test_engine_has_no_business_or_cli_tokens():
    offenders = []
    for path in _py_files(ENGINE_ROOT):
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path}: contains '{token}'")
    assert not offenders, "engine 不得含业务/渠道/CLI 字面量:\n" + "\n".join(offenders)
```

- [ ] **Step 2: 运行确认通过**

Run: `pytest tests/purity -q`
Expected: PASS（2 passed）— 证明 engine 至今保持纯净

- [ ] **Step 3: 写一条「负向自检」临时验证护栏有效（验证后删除）**

在 `claw_engine/engine/runtime/contracts.py` 顶部临时加注释 `# codex` → 运行 `pytest tests/purity -q` 应 FAIL → 删除该注释 → 复跑应 PASS。（不提交临时改动。）

- [ ] **Step 4: Commit**

```bash
git add tests/purity/test_engine_purity.py
git commit -m "test: enforce engine/adapters purity boundary in CI"
```

---

### Task 7: 最小 CLI Channel 端到端 smoke

**Files:**
- Create: `claw_engine/__main__.py`
- Test: `tests/test_cli_smoke.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_smoke.py
import subprocess, sys

def test_cli_runs_one_turn_with_fake_backend():
    out = subprocess.run(
        [sys.executable, "-m", "claw_engine", "--backend", "fake", "--prompt", "hello"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "echo: hello" in out.stdout
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_cli_smoke.py -q`
Expected: FAIL — `No module named claw_engine.__main__`

- [ ] **Step 3: 写实现（CLI 即一个最小 channel；注册 fake + codex）**

```python
# claw_engine/__main__.py
from __future__ import annotations
import argparse
import os
import sys
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.adapters.backends.codex.backend import CodexCliBackend


def _build_registry() -> EngineRegistry:
    reg = EngineRegistry()
    reg.register_backend("codex", lambda: CodexCliBackend())
    # 仅用于 smoke：内置一个 echo 假后端（不依赖真实 CLI）
    from claw_engine.engine.runtime.contracts import (
        AgentEvent, AgentEventKind, AgentRunResult, TokenUsage,
        BackendCapabilities, BackendHealth,
    )

    class _EchoBackend:
        name = "fake"
        def capabilities(self):
            return BackendCapabilities(False, False, False, False, ("none",))
        def healthcheck(self):
            return BackendHealth(ok=True)
        def run(self, req):
            text = f"echo: {req.prompt}"
            yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
            yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED,
                             result=AgentRunResult(None, text, TokenUsage()))

    reg.register_backend("fake", lambda: _EchoBackend())
    return reg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claw_engine")
    parser.add_argument("--backend", default="codex")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args(argv)

    engine = Engine(_build_registry())
    try:
        result = engine.run_turn(
            backend_name=args.backend, prompt=args.prompt, cwd=args.cwd, env=dict(os.environ),
        )
    except AgentRunFailed as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(result.final_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `pytest -q`
Expected: PASS（全部 task 测试通过）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/__main__.py tests/test_cli_smoke.py
git commit -m "feat: add minimal CLI channel and end-to-end smoke"
```

---

## Self-Review

**1. Spec coverage（P1 范围内）：**
- spec §3.1 类型 → Task 1 ✅
- spec §3.2 错误 taxonomy → Task 1（`AgentError/AgentErrorKind`）✅
- spec §3.3 硬规则（终态唯一、能力 gate、resume 透传、CLI 知识封装）→ Task 2（契约套件）+ Task 4（gate）+ Task 5（封装/resume）✅
- spec §3.4 backend_thread_id 透传 → Task 4/5（`backend_thread_id` 原样进出）✅
- spec §4.1/§4.2 组合根与依赖方向 → Task 3 ✅
- spec §4.3 纯净度断言 → Task 6 ✅
- **超出 P1 的部分**（session 持久化、claude backend、channels、skill、workflow、config center、identity、langfuse）→ 明确属 P2–P7，不在本计划。

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 均含完整可运行代码。✅

**3. Type consistency：** `AgentRunRequest.backend_thread_id`、`AgentEvent.backend_thread_id/result/error`、`AgentRunResult.backend_thread_id`、`CodeAgentBackend.{name,capabilities,run,healthcheck}`、`EngineRegistry.{register_backend,resolve_backend}`、`Engine.run_turn(...)`、`assert_valid_event_stream`、`CodexCliBackend(spawn=...)` 在各 task 间签名一致。✅

**已知小取舍（实现时注意）：**
- `_default_spawn` 的 `proc.wait` 放在生成器内，退出码在迭代结束后才确定——`run()` 先耗尽 `line_iter` 再调 `returncode()`，顺序正确。
- 纯净度 token 检查用子串匹配，`claw_engine/engine` 内的正常英文不含这些词；若未来误伤（如注释里出现 "claude"），按 spec §4.3 它本就该被拒。
