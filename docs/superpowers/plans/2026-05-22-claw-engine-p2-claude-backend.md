# claw_engine P2 — ClaudeCodeBackend + 双后端归一 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 实现 `ClaudeCodeBackend` 最小可用 walking skeleton，复用 L2 contract 与 `assert_valid_event_stream`，并抽出**共享 backend 契约用例**让 codex + claude 跑同一套行为契约，证明「双后端归一」。

**Architecture:** claude 后端住在 `adapters/backends/claude/`，把 `claude -p --output-format stream-json --verbose` 的 JSONL 事件流归一成统一 `AgentEvent`。把 codex/claude 重复的子进程 spawn（含 watchdog 超时）和错误构造抽到 `adapters/backends/_subprocess.py` / `_errors.py` 共享，先重构 codex 落到这两个 util（codex 保持 26 绿），再让 claude 复用。契约 hardening（PROTOCOL/TIMEOUT/BACKEND_CRASH）与 codex 同等级。

**Tech Stack:** Python 3.11+，pytest，ruff。注入式 spawn → 全部 hermetic，不依赖本机真实 codex/claude 登录态。

**前置参考：** P1 spec `docs/superpowers/specs/2026-05-22-codeagent-engine-design.md`（§3 contract）、P1 计划 `2026-05-22-claw-engine-v1-core.md`、现有 `claw_engine/adapters/backends/codex/backend.py`（contract hardening 参照）。

---

## 设计基线（实现前必读，非任务）

### Claude Code CLI 调用与 stream-json 事件
调用：`claude -p <prompt> --output-format stream-json --verbose [--resume <session_id>] [--model <model>] --dangerously-skip-permissions`
（`-p` 非交互；`--output-format stream-json` 需配 `--verbose`；`--dangerously-skip-permissions` 与 codex 的 sandbox-off 同级，spec 已标记为已知风险。）

逐行 JSONL，关心这几类（其余 type 前向兼容忽略）：

| claude 事件 | 关键字段 | 归一为 |
|---|---|---|
| `{"type":"system","subtype":"init","session_id":...}` | `session_id`（必需） | `THREAD_STARTED`（backend_thread_id=session_id） |
| `{"type":"assistant","message":{"content":[...]}}` | content blocks | 遍历 block：`text`→`MESSAGE_COMPLETED`；`tool_use`→`TOOL_CALL_COMPLETED` |
| `{"type":"user",...}`（tool_result 回填） | — | V1 忽略（前向兼容） |
| `{"type":"result","subtype":"success","result":...,"session_id":...,"usage":...}` | `result`/`session_id`(必需且须与 init 一致)/`is_error`/`usage` | `is_error=false`→`TURN_COMPLETED`(final_text=result，backend_thread_id=session_id)；`is_error=true`→`ERROR(BACKEND_CRASH)` |

### 错误映射（与 codex 同等级 contract hardening）
- 非 JSON / json 解析失败 → `ERROR(PROTOCOL)`，立即终止。
- 合法 JSON 未知 `type` → 忽略（前向兼容）。
- 已知 type 缺 contract 必需字段 → `ERROR(PROTOCOL)`：`system/init` 缺 `session_id`；`assistant` 缺 `message.content`（非 list）；`tool_use` block 缺 `name`；`result` 标 success 但缺 `result` 文本；`result` 缺 `session_id` 或与 `init.session_id` 不一致（防止 terminal `backend_thread_id` 悄悄变 None / 错 token）。
- `subprocess.TimeoutExpired`（读阶段，watchdog 触发）→ `ERROR(TIMEOUT, retriable)`。
- `OSError`/`SubprocessError` → `ERROR(BACKEND_CRASH)`。窄异常，不掩盖逻辑 bug。
- 子进程非零退出 → `ERROR(BACKEND_CRASH)`。

### resume 语义
- `backend_thread_id` == claude `session_id`，对引擎不透明；resume 时拼 `--resume <session_id>`。
- 跨 backend 不复用：由 L1 会话层的 `backend_name` 固定保证（spec §3.4）；backend 自身只把 `backend_thread_id` 当不透明 token 透传，不解析。

### capabilities（V1 诚实声明，不虚标）
`ClaudeCodeBackend`：`supports_resume=True`（--resume）、`supports_streaming=False`（V1 只发完整 MESSAGE_COMPLETED，不发 delta）、`supports_tools=True`（映射 tool_use）、`supports_mcp=False`（V1 不接 --mcp-config）、`auth_modes=("api_key","cli_login")`。

---

### Task 1: 抽出共享子进程/错误 util，重构 codex 落地（codex 保持绿）

> **纯机械抽取，零语义变更，独立 commit。** 只把 codex 现有的 `_default_spawn`/`SpawnFn`/`_protocol_error` 与内联的 timeout/crash 错误构造**原样搬**到共享 `_subprocess.py`/`_errors.py`，再让 codex import 复用。错误的 `kind`/`retriable`/`message` 必须与现状逐一对应（`protocol_error` retriable=False、`timeout_error` retriable=True、`crash_error` retriable=False），**不调整 codex 任何行为**。验收标准：`tests/test_codex_backend.py` 与全量测试**数目与结果完全不变**（现有测试已覆盖 codex hardening，是这次重构的安全网）。若任何 codex 测试变红，即说明动到了语义，必须回退重做。

**Files:**
- Create: `claw_engine/adapters/backends/_subprocess.py`
- Create: `claw_engine/adapters/backends/_errors.py`
- Modify: `claw_engine/adapters/backends/codex/backend.py`（改用共享 util）
- Test: 复用现有 `tests/test_codex_backend.py`（不改，必须仍全绿）

- [ ] **Step 1: 跑基线，确认 codex 当前全绿**

Run: `.venv/bin/pytest tests/test_codex_backend.py -q`
Expected: PASS（9 passed）

- [ ] **Step 2: 写共享 spawn util**

```python
# claw_engine/adapters/backends/_subprocess.py
from __future__ import annotations
import subprocess
import threading
from typing import Callable, Iterator, Mapping, Tuple

# spawn(argv, cwd, env, timeout_s) -> (stdout 行迭代器, 取退出码的可调用)
SpawnFn = Callable[[list, str, Mapping[str, str], int], Tuple[Iterator[str], Callable[[], int]]]


def default_spawn(argv, cwd, env, timeout_s):
    """启动子进程，逐行产出 stdout；watchdog 在 timeout_s 后 kill 并令迭代抛 TimeoutExpired。"""
    proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    timed_out = {"flag": False}

    def _on_timeout() -> None:
        timed_out["flag"] = True
        proc.kill()

    timer = threading.Timer(timeout_s, _on_timeout)
    timer.daemon = True
    timer.start()

    def lines() -> Iterator[str]:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield line.rstrip("\n")
        finally:
            timer.cancel()
            proc.wait()
        if timed_out["flag"]:
            raise subprocess.TimeoutExpired(argv, timeout_s)

    return lines(), (lambda: proc.returncode if proc.returncode is not None else 0)
```

- [ ] **Step 3: 写共享错误构造 util**

```python
# claw_engine/adapters/backends/_errors.py
from __future__ import annotations
from typing import Any, Mapping, Optional
from claw_engine.engine.runtime.contracts import AgentEvent, AgentEventKind, AgentError, AgentErrorKind


def _err(kind: AgentErrorKind, message: str, *, retriable: bool, raw: Optional[Mapping[str, Any]] = None) -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.ERROR,
                      error=AgentError(kind=kind, message=message, retriable=retriable, raw=raw))


def protocol_error(message: str, raw: Optional[Mapping[str, Any]] = None) -> AgentEvent:
    return _err(AgentErrorKind.PROTOCOL, message, retriable=False, raw=raw)


def timeout_error(message: str) -> AgentEvent:
    return _err(AgentErrorKind.TIMEOUT, message, retriable=True)


def crash_error(message: str) -> AgentEvent:
    return _err(AgentErrorKind.BACKEND_CRASH, message, retriable=False)
```

- [ ] **Step 4: 重构 codex/backend.py 使用共享 util**

将 `codex/backend.py` 顶部本地的 `_default_spawn`、`SpawnFn`、`_protocol_error` 删除，改为：
```python
from claw_engine.adapters.backends._subprocess import SpawnFn, default_spawn
from claw_engine.adapters.backends._errors import protocol_error, timeout_error, crash_error
```
- `__init__` 中 `self._spawn = spawn or default_spawn`
- 所有 `_protocol_error(...)` → `protocol_error(...)`
- `run()` 的 `except subprocess.TimeoutExpired:` 分支体改为 `yield timeout_error(f"codex 读取超时 ({req.timeout_s}s)"); return`
- `except (OSError, subprocess.SubprocessError) as exc:` 分支体改为 `yield crash_error(f"codex 子进程异常: {exc}"); return`
- 非零退出分支改为 `yield crash_error(f"codex exited with {code}"); return`
- 保留 codex 专属的 `_build_argv` 与事件归一逻辑不变。仍 `import subprocess`（TimeoutExpired/SubprocessError 捕获需要）。

- [ ] **Step 5: 跑 codex 测试 + 全量，确认无回归**

Run: `.venv/bin/pytest tests/test_codex_backend.py -q && .venv/bin/pytest -q`
Expected: codex 9 passed；全量仍 26 passed

- [ ] **Step 6: ruff + purity**

Run: `.venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: All checks passed；purity 2 passed

- [ ] **Step 7: Commit**

```bash
git add claw_engine/adapters/backends/_subprocess.py claw_engine/adapters/backends/_errors.py claw_engine/adapters/backends/codex/backend.py
git commit -m "refactor: extract shared spawn/error utils for backends; codex uses them"
```

---

### Task 2: 共享 backend 契约用例（参数化），先接 codex

**Files:**
- Create: `claw_engine/engine/runtime/contract_suite.py`（已存在，**不改**——仅复用 `assert_valid_event_stream`）
- Create: `tests/contract/backend_contract.py`（共享行为断言）
- Create: `tests/contract/codex_fixtures.py`（codex 的 canned 流 + make_backend）
- Create: `tests/contract/test_backend_contract.py`（参数化，先只含 codex）

- [ ] **Step 1: 写共享契约断言（先写测试 helper，由下一步的参数化测试驱动）**

```python
# tests/contract/backend_contract.py
"""跨后端共享的行为契约断言。codex / claude 用各自 canned 流跑同一套。"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional
from claw_engine.engine.runtime.contracts import (
    AgentEventKind, AgentErrorKind, AgentRunRequest, CodeAgentBackend,
)
from claw_engine.engine.runtime.contract_suite import assert_valid_event_stream

# make_backend(lines, returncode, raise_exc) -> backend（注入式 spawn）
MakeBackend = Callable[[Optional[list], int, Optional[BaseException]], CodeAgentBackend]


@dataclass(frozen=True)
class BackendContractFixture:
    name: str
    make_backend: MakeBackend
    success_lines: list
    expected_final_text: str
    expected_thread_id: str
    malformed_lines: list  # 含一条非 JSON 行，应触发 PROTOCOL


def _run(backend: CodeAgentBackend, **kw):
    return list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, **kw)))


def assert_success(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(fx.success_lines, 0, None))
    assert_valid_event_stream(events)
    assert events[0].kind is AgentEventKind.THREAD_STARTED
    assert events[0].backend_thread_id == fx.expected_thread_id
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == fx.expected_final_text
    assert events[-1].result.backend_thread_id == fx.expected_thread_id


def assert_nonzero_exit_is_backend_crash(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend([], 1, None))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR
    assert events[-1].error.kind is AgentErrorKind.BACKEND_CRASH


def assert_malformed_is_protocol(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(fx.malformed_lines, 0, None))
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.PROTOCOL
    assert not any(e.kind is AgentEventKind.TURN_COMPLETED for e in events)


def assert_timeout_is_error(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(None, 0, subprocess.TimeoutExpired("cli", 1)), timeout_s=1)
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.TIMEOUT


def assert_crash_is_backend_crash(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(None, 0, OSError("broken pipe")))
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.BACKEND_CRASH
```

- [ ] **Step 2: 写 codex fixture**

```python
# tests/contract/codex_fixtures.py
from __future__ import annotations
from typing import Optional
from claw_engine.adapters.backends.codex.backend import CodexCliBackend
from tests.contract.backend_contract import BackendContractFixture


class _RaisingIter:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
    def __iter__(self) -> "_RaisingIter":
        return self
    def __next__(self) -> str:
        raise self._exc


def _make(lines: Optional[list], returncode: int, raise_exc: Optional[BaseException]) -> CodexCliBackend:
    def spawn(argv, cwd, env, timeout_s):
        it = _RaisingIter(raise_exc) if raise_exc is not None else iter(lines or [])
        return it, (lambda: returncode)
    return CodexCliBackend(spawn=spawn)


SUCCESS_LINES = [
    '{"type":"thread.started","thread_id":"th_123"}',
    '{"type":"item.completed","item":{"type":"tool_call","name":"shell","input":{"cmd":"ls"}}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}',
]
MALFORMED_LINES = [
    '{"type":"thread.started","thread_id":"th_1"}',
    'not json at all {{{',
    '{"type":"item.completed","item":{"type":"agent_message","text":"unreachable"}}',
]

FIXTURE = BackendContractFixture(
    name="codex", make_backend=_make,
    success_lines=SUCCESS_LINES, expected_final_text="final answer", expected_thread_id="th_123",
    malformed_lines=MALFORMED_LINES,
)
```

- [ ] **Step 3: 写参数化契约测试（先只 codex），跑 PASS**

```python
# tests/contract/test_backend_contract.py
import pytest
from tests.contract import backend_contract as bc
from tests.contract import codex_fixtures

FIXTURES = [codex_fixtures.FIXTURE]

@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_success_contract(fx):
    bc.assert_success(fx)

@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_nonzero_exit_is_backend_crash(fx):
    bc.assert_nonzero_exit_is_backend_crash(fx)

@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_malformed_is_protocol(fx):
    bc.assert_malformed_is_protocol(fx)

@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_timeout_is_error(fx):
    bc.assert_timeout_is_error(fx)

@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_crash_is_backend_crash(fx):
    bc.assert_crash_is_backend_crash(fx)
```

Run: `.venv/bin/pytest tests/contract/test_backend_contract.py -q`
Expected: PASS（5 passed，ids 全是 `codex`）

- [ ] **Step 4: 从 `tests/test_codex_backend.py` 删除已被共享套件覆盖的行为用例**

删除其中 `test_codex_success_normalizes_to_contract`、`test_codex_nonzero_exit_maps_to_error`、`test_codex_malformed_json_maps_to_protocol_error`、`test_codex_timeout_maps_to_error`、`test_codex_subprocess_exception_maps_to_backend_crash`（这些已由参数化共享套件覆盖）。
**保留** codex 专属 schema 用例：`test_codex_resume_passes_thread_id_in_argv`、`test_codex_thread_started_missing_thread_id_is_protocol_error`、`test_codex_item_completed_missing_type_is_protocol_error`、`test_codex_unknown_type_is_ignored_forward_compat`。同步清理因删测试而不再使用的 import / 常量（`_RaisingIter` 若不再用则删，`CANNED_OK`/`CANNED_MALFORMED` 按保留用例需要决定）。

- [ ] **Step 5: 全量 + ruff，确认无回归无 lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests`
Expected: 全量 PASS（数目随迁移调整，无 FAIL）；All checks passed

- [ ] **Step 6: Commit**

```bash
git add tests/contract/backend_contract.py tests/contract/codex_fixtures.py tests/contract/test_backend_contract.py tests/test_codex_backend.py
git commit -m "test: add shared parametrized backend contract suite (codex wired in)"
```

---

### Task 3: ClaudeCodeBackend 成功路径 skeleton + 接入共享套件

**Files:**
- Create: `claw_engine/adapters/backends/claude/__init__.py`
- Create: `claw_engine/adapters/backends/claude/backend.py`
- Create: `tests/contract/claude_fixtures.py`
- Test: `tests/test_claude_backend.py`（claude 专属）
- Modify: `tests/contract/test_backend_contract.py`（把 claude 加入 FIXTURES）

- [ ] **Step 1: 写 claude 专属成功 + argv 测试（先 RED）**

```python
# tests/test_claude_backend.py
from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind

SUCCESS_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_abc","model":"claude-x"}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"ls"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tu1","content":"file.txt"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"final answer"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"final answer","session_id":"sess_abc","usage":{"input_tokens":100,"output_tokens":20}}',
]

def _spawn_factory(lines, returncode=0):
    def spawn(argv, cwd, env, timeout_s):
        return iter(lines), (lambda: returncode)
    return spawn

def test_claude_success_maps_to_contract():
    backend = ClaudeCodeBackend(spawn=_spawn_factory(SUCCESS_LINES))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert events[0].kind is AgentEventKind.THREAD_STARTED
    assert events[0].backend_thread_id == "sess_abc"
    assert any(e.kind is AgentEventKind.TOOL_CALL_COMPLETED and e.tool.name == "Bash" for e in events)
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "final answer"
    assert events[-1].result.backend_thread_id == "sess_abc"

def test_claude_resume_passes_session_id_in_argv():
    captured = {}
    def spawn(argv, cwd, env, timeout_s):
        captured["argv"] = argv
        return iter(SUCCESS_LINES), (lambda: 0)
    backend = ClaudeCodeBackend(spawn=spawn)
    list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, backend_thread_id="sess_9")))
    assert "--resume" in captured["argv"]
    assert "sess_9" in captured["argv"]
    assert "--output-format" in captured["argv"] and "stream-json" in captured["argv"]

def test_claude_capabilities_are_honest():
    caps = ClaudeCodeBackend().capabilities()
    assert caps.supports_resume is True
    assert caps.supports_streaming is False
    assert caps.supports_tools is True
    assert caps.supports_mcp is False   # V1 不接 --mcp-config，不虚标
```

- [ ] **Step 2: 跑确认 FAIL（ModuleNotFoundError: ...claude.backend）**

Run: `.venv/bin/pytest tests/test_claude_backend.py -q`
Expected: FAIL

- [ ] **Step 3: 写 ClaudeCodeBackend（成功路径 + 复用共享 util）**

```python
# claw_engine/adapters/backends/claude/backend.py
from __future__ import annotations
import json
import shutil
import subprocess
from typing import Iterator, Optional
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest, AgentRunResult,
    BackendCapabilities, BackendHealth, ToolEvent, TokenUsage,
)
from claw_engine.adapters.backends._subprocess import SpawnFn, default_spawn
from claw_engine.adapters.backends._errors import protocol_error, timeout_error, crash_error


class ClaudeCodeBackend:
    name = "claude"

    def __init__(self, command: str = "claude", spawn: Optional[SpawnFn] = None) -> None:
        self._command = command
        self._spawn = spawn or default_spawn

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_resume=True, supports_streaming=False,
            supports_tools=True, supports_mcp=False, auth_modes=("api_key", "cli_login"),
        )

    def healthcheck(self) -> BackendHealth:
        ok = shutil.which(self._command) is not None
        return BackendHealth(ok=ok, detail="" if ok else "claude not on PATH")

    def _build_argv(self, req: AgentRunRequest) -> list:
        argv = [self._command, "-p", req.prompt,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions"]
        if req.backend_thread_id:
            argv += ["--resume", req.backend_thread_id]
        if req.model:
            argv += ["--model", req.model]
        return argv

    def run(self, req: AgentRunRequest) -> Iterator[AgentEvent]:
        argv = self._build_argv(req)
        line_iter, returncode = self._spawn(argv, req.cwd, req.env, req.timeout_s)
        session_id = req.backend_thread_id
        final_text = ""
        try:
            for raw_line in line_iter:
                if not raw_line.strip():
                    continue
                try:
                    evt = json.loads(raw_line)
                except json.JSONDecodeError:
                    yield protocol_error(f"无法解析 claude 输出行: {raw_line[:200]!r}")
                    return
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "init":
                    sid = evt.get("session_id")
                    if not sid:
                        yield protocol_error("system/init 缺少 session_id", evt)
                        return
                    session_id = sid
                    yield AgentEvent(kind=AgentEventKind.THREAD_STARTED,
                                     backend_thread_id=session_id, raw=evt)
                elif etype == "assistant":
                    message = evt.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        yield protocol_error("assistant 缺少 message.content", evt)
                        return
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            if "text" not in block:
                                yield protocol_error("text block 缺少 text", evt)
                                return
                            final_text = block.get("text") or ""
                            yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED,
                                             text=final_text, raw=evt)
                        elif btype == "tool_use":
                            name = block.get("name")
                            if not name:
                                yield protocol_error("tool_use 缺少 name", evt)
                                return
                            yield AgentEvent(
                                kind=AgentEventKind.TOOL_CALL_COMPLETED,
                                tool=ToolEvent(name=name, input=block.get("input")),
                                raw=evt,
                            )
                        # 未知 block.type → 前向兼容忽略
                elif etype == "result":
                    if evt.get("is_error"):
                        yield crash_error(f"claude result error: {evt.get('subtype', 'unknown')}")
                        return
                    if "result" not in evt:
                        yield protocol_error("result(success) 缺少 result 文本", evt)
                        return
                    rsid = evt.get("session_id")
                    if not rsid:
                        yield protocol_error("result 缺少 session_id", evt)
                        return
                    if session_id is not None and rsid != session_id:
                        yield protocol_error(
                            f"result.session_id 与 init 不一致: {rsid!r} != {session_id!r}", evt)
                        return
                    session_id = rsid
                    final_text = evt.get("result") or final_text
                    usage = evt.get("usage") or {}
                    yield AgentEvent(
                        kind=AgentEventKind.TURN_COMPLETED,
                        backend_thread_id=session_id,
                        result=AgentRunResult(
                            backend_thread_id=session_id, final_text=final_text,
                            usage=TokenUsage(
                                input_tokens=int(usage.get("input_tokens", 0) or 0),
                                output_tokens=int(usage.get("output_tokens", 0) or 0),
                            ),
                        ),
                    )
                    return
                # 未知顶层 type / user(tool_result) → 前向兼容忽略
            code = returncode()
        except subprocess.TimeoutExpired:
            yield timeout_error(f"claude 读取超时 ({req.timeout_s}s)")
            return
        except (OSError, subprocess.SubprocessError) as exc:
            yield crash_error(f"claude 子进程异常: {exc}")
            return
        # 走到这里说明流结束但没有 result 终态
        if code != 0:
            yield crash_error(f"claude exited with {code}")
            return
        yield protocol_error("claude 流结束但缺少 result 终态事件")
```

> 说明：claude 以 `result` 事件为成功终态（命中即 `return`）。若流正常结束（exit 0）却从未出现 `result`，按协议破损处理（`PROTOCOL`），不静默降级成空成功——与 codex「不静默降级」一致。

- [ ] **Step 4: 跑 claude 专属测试，确认 PASS（3 passed）**

Run: `.venv/bin/pytest tests/test_claude_backend.py -q`
Expected: PASS

- [ ] **Step 5: 写 claude fixture 并接入共享套件**

```python
# tests/contract/claude_fixtures.py
from __future__ import annotations
from typing import Optional
from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
from tests.contract.backend_contract import BackendContractFixture


class _RaisingIter:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
    def __iter__(self) -> "_RaisingIter":
        return self
    def __next__(self) -> str:
        raise self._exc


def _make(lines: Optional[list], returncode: int, raise_exc: Optional[BaseException]) -> ClaudeCodeBackend:
    def spawn(argv, cwd, env, timeout_s):
        it = _RaisingIter(raise_exc) if raise_exc is not None else iter(lines or [])
        return it, (lambda: returncode)
    return ClaudeCodeBackend(spawn=spawn)


SUCCESS_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_abc","model":"claude-x"}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"final answer"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"final answer","session_id":"sess_abc"}',
]
MALFORMED_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_abc"}',
    'not json at all {{{',
    '{"type":"result","subtype":"success","is_error":false,"result":"unreachable","session_id":"sess_abc"}',
]

FIXTURE = BackendContractFixture(
    name="claude", make_backend=_make,
    success_lines=SUCCESS_LINES, expected_final_text="final answer", expected_thread_id="sess_abc",
    malformed_lines=MALFORMED_LINES,
)
```

修改 `tests/contract/test_backend_contract.py`：
```python
from tests.contract import codex_fixtures, claude_fixtures
FIXTURES = [codex_fixtures.FIXTURE, claude_fixtures.FIXTURE]
```

- [ ] **Step 6: 跑共享套件，确认 codex+claude 都过（10 passed：5 用例 × 2 后端）**

Run: `.venv/bin/pytest tests/contract/test_backend_contract.py -q`
Expected: PASS（10 passed，ids 含 `codex` 与 `claude`）

- [ ] **Step 7: 全量 + ruff + purity**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 全量 PASS；All checks passed；purity 2 passed（claude 在 adapters，engine 仍纯净）

- [ ] **Step 8: Commit**

```bash
git add claw_engine/adapters/backends/claude tests/test_claude_backend.py tests/contract/claude_fixtures.py tests/contract/test_backend_contract.py
git commit -m "feat: add ClaudeCodeBackend (stream-json), wire into shared contract suite"
```

---

### Task 4: Claude stream-json schema —— behavior-lock 测试（regression 锁定，非 TDD）

> **本任务不是 red→green TDD task，而是 behavior-lock（回归锁定）。** 理由：claude 的 field-level schema 守卫（缺 session_id / 缺 content / tool_use 缺 name / result 缺 session_id 等）与 stream-json 解析逻辑高度内聚，在 Task 3 与映射逻辑一起实现更自然、可读性更好，不宜人为拆成无守卫的中间态。因此 Task 3 已实现这些守卫，本任务用测试**锁定**它们、防回归。
>
> 预期这些测试**直接 PASS**（实现已在 Task 3）。若任一 FAIL，说明 Task 3 实现与 spec 不符——回到 `claude/backend.py` 修正对应分支后复跑，不要改测试去迁就实现。
>
> （注：red→green 纪律在 Task 2/3/5 的新增能力上仍严格执行；仅本 schema-lock 任务例外，且已显式声明。）

**Files:**
- Modify: `tests/test_claude_backend.py`

- [ ] **Step 1: 追加 claude schema 测试**

```python
from claw_engine.engine.runtime.contracts import AgentErrorKind

def _events(lines):
    backend = ClaudeCodeBackend(spawn=_spawn_factory(lines))
    return list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))

def test_claude_init_missing_session_id_is_protocol():
    ev = _events(['{"type":"system","subtype":"init","model":"x"}'])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL

def test_claude_assistant_missing_content_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{}}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL

def test_claude_tool_use_missing_name_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t","input":{}}]}}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL

def test_claude_result_error_maps_to_backend_crash():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"s1"}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.BACKEND_CRASH

def test_claude_unknown_type_is_ignored_forward_compat():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"some_future_event","x":1}',
        '{"type":"assistant","message":{"content":[{"type":"thinking","text":"hmm"}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","session_id":"s1"}',
    ])
    assert ev[-1].kind is AgentEventKind.TURN_COMPLETED
    assert ev[-1].result.final_text == "done"

def test_claude_stream_without_result_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL

def test_claude_result_missing_session_id_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done"}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL

def test_claude_result_session_id_mismatch_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","session_id":"s2"}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL
```

- [ ] **Step 2: 跑确认 PASS（实现已在 Task 3）**

Run: `.venv/bin/pytest tests/test_claude_backend.py -q`
Expected: PASS（3 + 8 = 11 passed）。若某条 FAIL，到 `claude/backend.py` 修正对应分支后复跑。

- [ ] **Step 3: 全量 + ruff**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests`
Expected: 全量 PASS；All checks passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_claude_backend.py
git commit -m "test: lock claude stream-json schema PROTOCOL/forward-compat boundaries"
```

---

### Task 5: 注册 claude 到 CLI（hermetic）+ 最终回归

**Files:**
- Modify: `claw_engine/__main__.py`
- Test: `tests/test_cli_smoke.py`（已存在，仍用 fake backend；新增 claude 注册存在性测试）

- [ ] **Step 1: 写「claude 已注册且不依赖真实登录」测试（先 RED）**

```python
# 追加到 tests/test_cli_smoke.py
def test_cli_registers_codex_and_claude_backends():
    from claw_engine.__main__ import _build_registry
    reg = _build_registry()
    # 解析不报错即说明已注册（构造不触发子进程/登录）
    assert reg.resolve_backend("codex").name == "codex"
    assert reg.resolve_backend("claude").name == "claude"
    assert reg.resolve_backend("fake").name == "fake"
```

- [ ] **Step 2: 跑确认 FAIL（claude 未注册 → BackendNotRegistered）**

Run: `.venv/bin/pytest tests/test_cli_smoke.py -q`
Expected: FAIL

- [ ] **Step 3: 在 `_build_registry()` 注册 claude**

在 `claw_engine/__main__.py` 的 `_build_registry()` 中，于注册 codex 之后加：
```python
    from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
    reg.register_backend("claude", lambda: ClaudeCodeBackend())
```
（`__main__.py` 在包根、非 `engine/` 下，允许 import adapters；注册是惰性 factory，不会触发子进程/登录态。smoke 仍用 `--backend fake`，不依赖真实 claude。）

- [ ] **Step 4: 跑 CLI 测试 + 手动 smoke（仍 fake，不碰真实 claude）**

Run: `.venv/bin/pytest tests/test_cli_smoke.py -q && .venv/bin/python -m claw_engine --backend fake --prompt hello`
Expected: PASS；打印 `echo: hello`

- [ ] **Step 5: 最终全量 + ruff + purity（双后端归一验收）**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 全量 PASS；All checks passed；purity 2 passed（engine 仍零 CLI 字面量）

- [ ] **Step 6: Commit**

```bash
git add claw_engine/__main__.py tests/test_cli_smoke.py
git commit -m "feat: register ClaudeCodeBackend in CLI registry (hermetic smoke unchanged)"
```

---

## Self-Review

**1. 需求覆盖（对照你 7 条）：**
1. claude stream-json 事件映射（assistant/text+tool_use、result/session_id、system/init）→ Task 3 Step 3 + 设计基线表 ✅
2. resume：`backend_thread_id`↔claude `session_id`/`--resume`，跨 backend 不复用（backend 透传不解析 + L1 backend_name 固定）→ 设计基线 + Task 3 argv 测试 ✅
3. PROTOCOL/TIMEOUT/BACKEND_CRASH 与 codex 同级 → 共享 `_errors.py` + Task 3 实现 + Task 4 schema 测试 ✅
4. capabilities 诚实（supports_mcp=False、不虚标）→ Task 3 `test_claude_capabilities_are_honest` ✅
5. 抽共享契约用例 codex+claude 都跑 + claude 自有 schema 测试 → Task 2（共享套件）+ Task 3 接入 + Task 4 ✅
6. CLI smoke 用 fake spawn 不依赖真实 claude 登录 → Task 5（注册惰性 factory，smoke 仍 `--backend fake`）✅
7. purity gate engine 零 CLI 字面量 → 每个改 adapters 的 task 末尾跑 `tests/purity`；claude 在 `adapters/backends/claude/` ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码。✅

**3. 类型/签名一致性：** `BackendContractFixture`(name/make_backend/success_lines/expected_final_text/expected_thread_id/malformed_lines)、`MakeBackend(lines,returncode,raise_exc)`、`default_spawn`/`SpawnFn`、`protocol_error/timeout_error/crash_error`、`ClaudeCodeBackend(command,spawn)` 在各 task 间一致。codex 重构后仍满足 `CodeAgentBackend` Protocol。✅

**已知取舍（实现注意）：**
- claude `result` 为成功终态；流缺 `result` → PROTOCOL（不静默空成功），与 codex「不降级」一致。
- V1 不映射 `user`/tool_result 的工具输出（`ToolEvent.output` 暂为 None），属最小 skeleton；后续可补。
- `--dangerously-skip-permissions` 与 codex sandbox-off 同级风险，spec 已记录；隔离策略强化属后续 plan。
