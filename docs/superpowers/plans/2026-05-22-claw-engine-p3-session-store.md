# claw_engine P3 — L1 ConversationService + L5 SessionStore（多轮续接）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 落地会话持久化：`(workspace_id, channel, external_thread_key)` → 一条 Session（含 `backend_name` + nullable `backend_thread_id` + 轮次 + 去重），并用 L1 `ConversationService` 串起「续接 → run_turn → 回填 backend_thread_id」，实现多轮续接。

**Architecture:** L5 `SessionStore`（抽象 Protocol + memory + sqlite 两实现）+ L1 `ConversationService`（包 `Engine` + `SessionStore`，`Engine.run_turn` 保持无状态）。`Session` 不可变（frozen dataclass，`with_turn()` 产新副本）。sqlite 全参数化查询、表名为常量。两实现跑同一套共享 store 契约（呼应 P2 的 backend 契约套件）。

**Tech Stack:** Python 3.11+，标准库 `sqlite3`/`uuid`，pytest，ruff。全 hermetic（memory store 或 sqlite `:memory:`/tmp 文件）。

**前置参考：** spec `2026-05-22-codeagent-engine-design.md`（§3.4 Session 入口 vs backend resume 句柄、§5 数据模型、§6.3 安全），P1/P2 计划。

---

## 设计基线（实现前必读，非任务）

### 三个已拍板决策
1. **L1 入口** = 新增 `ConversationService.handle(...)`，包 `Engine`+`SessionStore`；`Engine.run_turn` 不变（无状态/纯）。
2. **max_rounds 超限** → 抛 `SessionRoundsExceeded`（不自动开新会话、不软警告），由上层决定提示。
3. **去重**（`processed_message_ids`）**纳入 P3**：`is_processed` 命中则抛 `DuplicateMessage`，不重复 run。

### 边界（P3 不做，避免越界影响后续层）
- **不**解析 workspace→cwd/env（L4，P5）：`cwd`/`env` 仍由调用方传入 `handle()`，透传给 `run_turn`。
- **不**接入真实 channel（L0，P6）：`channel`/`external_thread_key`/`message_id` 作为普通入参；CLI 暂不改（仍单轮 run_turn），ConversationService 仅由测试驱动。
- **不**做 workspace→cwd 隔离/RBAC（P5/P6）。

### Session 模型与 key（spec §3.4/§5）
- 主键 `session_id`（uuid4 hex）；唯一键 `(workspace_id, channel, external_thread_key)`（用户会话入口）。
- `backend_name` 创建时确定、之后固定（切 backend = 换 external_thread_key 开新会话；同 key 永远同 session，`get_or_create` 对已存在会话**忽略**新传入的 backend_name/max_rounds，existing wins）。
- `backend_thread_id` nullable，首轮为 None，run_turn 后回填，对引擎不透明。
- `round_count` 每轮 +1；`last_active` epoch 秒。

### 安全（spec §6.3）
- sqlite **全部参数化查询**（值用 `?` 占位），表名为源码常量（不接受外部表名）。
- `processed_message` 用 `(session_id, message_id)` 复合主键 + `INSERT OR IGNORE`，天然去重且有界于查询。
- **去重作用域**：P3 去重**只保证单进程顺序调用下的幂等**（check 与 mark 之间无锁）。并发场景的 reserve-before-run（先占位再执行）、跨 session 的全局 message-id 作用域，留到 P6/channel 适配或生产 store——P3 已明确不接真实 channel、不处理并发，故此范围足够。

---

### Task 1: SessionStore 契约类型（Session + Protocol）

**Files:**
- Create: `claw_engine/engine/persistence/__init__.py`
- Create: `claw_engine/engine/persistence/contracts.py`
- Test: `tests/test_session_model.py`

- [ ] **Step 1: 写失败测试（Session 不可变 + with_turn 产新副本）**

```python
# tests/test_session_model.py
import dataclasses
import pytest
from claw_engine.engine.persistence.contracts import Session

def _session(**kw):
    base = dict(session_id="s1", workspace_id="w", channel="c",
                external_thread_key="t", backend_name="fake", max_rounds=50)
    base.update(kw)
    return Session(**base)

def test_session_is_frozen():
    s = _session()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.round_count = 5

def test_with_turn_returns_new_copy_and_bumps_round():
    s = _session(round_count=0, backend_thread_id=None)
    s2 = s.with_turn(backend_thread_id="bt_1", now=123.0)
    assert s2 is not s
    assert s.round_count == 0 and s.backend_thread_id is None        # 原对象不变
    assert s2.round_count == 1 and s2.backend_thread_id == "bt_1"
    assert s2.last_active == 123.0
    assert s2.session_id == s.session_id                              # 其余字段保留

def test_defaults():
    s = _session()
    assert s.backend_thread_id is None
    assert s.round_count == 0
    assert s.last_active == 0.0
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_session_model.py -q`
Expected: FAIL（ModuleNotFoundError: ...persistence.contracts）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/persistence/contracts.py
from __future__ import annotations
import time
from dataclasses import dataclass, replace
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Session:
    session_id: str
    workspace_id: str
    channel: str
    external_thread_key: str
    backend_name: str
    backend_thread_id: Optional[str] = None
    round_count: int = 0
    max_rounds: int = 50
    last_active: float = 0.0

    def with_turn(self, *, backend_thread_id: Optional[str], now: Optional[float] = None) -> "Session":
        """记录一轮后的新副本（不可变）：轮次 +1，回填 backend_thread_id，更新 last_active。"""
        return replace(
            self,
            backend_thread_id=backend_thread_id,
            round_count=self.round_count + 1,
            last_active=now if now is not None else time.time(),
        )


@runtime_checkable
class SessionStore(Protocol):
    def get_or_create(self, *, workspace_id: str, channel: str, external_thread_key: str,
                      backend_name: str, max_rounds: int) -> Session: ...

    def save(self, session: Session) -> None:
        """持久化对一个**已存在**会话（即先前由本 store `get_or_create` 返回过的
        `session_id`）的更新。契约**不**保证为任意新 session_id 创建记录——
        新建会话只能走 `get_or_create`。这样 memory(upsert) 与 sqlite(update-only)
        在本契约范围内行为一致；契约测试只覆盖此路径。"""
        ...

    def is_processed(self, session_id: str, message_id: str) -> bool: ...
    def mark_processed(self, session_id: str, message_id: str) -> None: ...
```

- [ ] **Step 4: 跑确认 PASS（3 passed）+ purity**

Run: `.venv/bin/pytest tests/test_session_model.py -q && .venv/bin/pytest tests/purity -q`
Expected: 3 passed；purity 2 passed（persistence 在 engine/ 下但无 CLI 字面量）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/persistence/__init__.py claw_engine/engine/persistence/contracts.py tests/test_session_model.py
git commit -m "feat: add Session model and SessionStore contract"
```

---

### Task 2: MemorySessionStore + 共享 store 契约套件

**Files:**
- Create: `claw_engine/engine/persistence/memory_store.py`
- Create: `tests/contract/sessionstore_contract.py`（共享行为断言）
- Create: `tests/contract/test_sessionstore_contract.py`（参数化，先只 memory）

- [ ] **Step 1: 写共享 store 契约断言**

```python
# tests/contract/sessionstore_contract.py
"""跨实现共享的 SessionStore 行为契约。memory / sqlite 用同一套。"""
from __future__ import annotations
from typing import Callable
from claw_engine.engine.persistence.contracts import SessionStore

MakeStore = Callable[[], SessionStore]

_KW = dict(workspace_id="w", channel="c", external_thread_key="t", backend_name="fake", max_rounds=50)


def assert_get_or_create_idempotent(make_store: MakeStore) -> None:
    store = make_store()
    s1 = store.get_or_create(**_KW)
    s2 = store.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                             backend_name="other", max_rounds=10)
    assert s1.session_id == s2.session_id
    assert s2.backend_name == "fake"   # existing wins
    assert s2.max_rounds == 50


def assert_different_key_different_session(make_store: MakeStore) -> None:
    store = make_store()
    a = store.get_or_create(**{**_KW, "external_thread_key": "t1"})
    b = store.get_or_create(**{**_KW, "external_thread_key": "t2"})
    assert a.session_id != b.session_id


def assert_save_persists_turn(make_store: MakeStore) -> None:
    store = make_store()
    s = store.get_or_create(**_KW)
    store.save(s.with_turn(backend_thread_id="bt_1", now=123.0))
    again = store.get_or_create(**_KW)
    assert again.backend_thread_id == "bt_1"
    assert again.round_count == 1
    assert again.last_active == 123.0


def assert_dedup_tracks_message_ids(make_store: MakeStore) -> None:
    store = make_store()
    s = store.get_or_create(**_KW)
    assert not store.is_processed(s.session_id, "m1")
    store.mark_processed(s.session_id, "m1")
    assert store.is_processed(s.session_id, "m1")
    assert not store.is_processed(s.session_id, "m2")
    store.mark_processed(s.session_id, "m1")  # 幂等，不报错
```

- [ ] **Step 2: 写参数化测试（先只 memory），跑 FAIL（memory_store 未建）**

```python
# tests/contract/test_sessionstore_contract.py
import pytest
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from tests.contract import sessionstore_contract as sc

STORES = [("memory", lambda: MemorySessionStore())]

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_get_or_create_idempotent(name, make_store):
    sc.assert_get_or_create_idempotent(make_store)

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_different_key_different_session(name, make_store):
    sc.assert_different_key_different_session(make_store)

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_save_persists_turn(name, make_store):
    sc.assert_save_persists_turn(make_store)

@pytest.mark.parametrize("name,make_store", STORES, ids=[n for n, _ in STORES])
def test_dedup_tracks_message_ids(name, make_store):
    sc.assert_dedup_tracks_message_ids(make_store)
```

Run: `.venv/bin/pytest tests/contract/test_sessionstore_contract.py -q`
Expected: FAIL（ModuleNotFoundError: ...memory_store）

- [ ] **Step 3: 写 MemorySessionStore**

```python
# claw_engine/engine/persistence/memory_store.py
from __future__ import annotations
import uuid
from typing import Dict, Optional, Set, Tuple
from claw_engine.engine.persistence.contracts import Session


class MemorySessionStore:
    def __init__(self) -> None:
        self._by_id: Dict[str, Session] = {}
        self._key_to_id: Dict[Tuple[str, str, str], str] = {}
        self._processed: Set[Tuple[str, str]] = set()

    def get_or_create(self, *, workspace_id: str, channel: str, external_thread_key: str,
                      backend_name: str, max_rounds: int) -> Session:
        key = (workspace_id, channel, external_thread_key)
        sid: Optional[str] = self._key_to_id.get(key)
        if sid is not None:
            return self._by_id[sid]
        session = Session(
            session_id=uuid.uuid4().hex, workspace_id=workspace_id, channel=channel,
            external_thread_key=external_thread_key, backend_name=backend_name,
            max_rounds=max_rounds,
        )
        self._by_id[session.session_id] = session
        self._key_to_id[key] = session.session_id
        return session

    def save(self, session: Session) -> None:
        self._by_id[session.session_id] = session
        self._key_to_id[(session.workspace_id, session.channel, session.external_thread_key)] = session.session_id

    def is_processed(self, session_id: str, message_id: str) -> bool:
        return (session_id, message_id) in self._processed

    def mark_processed(self, session_id: str, message_id: str) -> None:
        self._processed.add((session_id, message_id))
```

- [ ] **Step 4: 跑确认 PASS（4 passed，ids=memory）+ 全量 + ruff + purity**

Run: `.venv/bin/pytest tests/contract/test_sessionstore_contract.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 4 passed；全量 PASS；All checks passed；purity 2 passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/persistence/memory_store.py tests/contract/sessionstore_contract.py tests/contract/test_sessionstore_contract.py
git commit -m "feat: add MemorySessionStore and shared SessionStore contract suite"
```

---

### Task 3: SqliteSessionStore + 接入共享套件（memory+sqlite 同契约）

**Files:**
- Create: `claw_engine/engine/persistence/sqlite_store.py`
- Modify: `tests/contract/test_sessionstore_contract.py`（STORES 加 sqlite）
- Test: `tests/test_sqlite_store.py`（sqlite 专属：参数化查询/持久化到文件）

- [ ] **Step 1: 写 sqlite 专属测试（先 RED）**

```python
# tests/test_sqlite_store.py
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore

def test_sqlite_persists_across_connections(tmp_path):
    db = str(tmp_path / "sessions.db")
    store1 = SqliteSessionStore(db)
    s = store1.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                             backend_name="fake", max_rounds=50)
    store1.save(s.with_turn(backend_thread_id="bt_9", now=42.0))
    store1.mark_processed(s.session_id, "m1")

    # 新连接（模拟重启）应看到已持久化的会话与去重记录
    store2 = SqliteSessionStore(db)
    again = store2.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                                 backend_name="ignored", max_rounds=1)
    assert again.session_id == s.session_id
    assert again.backend_thread_id == "bt_9"
    assert again.round_count == 1
    assert again.backend_name == "fake"   # 既有会话不被覆盖
    assert store2.is_processed(s.session_id, "m1")

def test_sqlite_uses_parameterized_queries():
    # 含特殊字符的输入不应破坏 SQL（参数化保证）
    store = SqliteSessionStore(":memory:")
    s = store.get_or_create(workspace_id="w'; DROP TABLE session; --", channel="c",
                            external_thread_key="t", backend_name="fake", max_rounds=50)
    assert s.session_id
    again = store.get_or_create(workspace_id="w'; DROP TABLE session; --", channel="c",
                                external_thread_key="t", backend_name="fake", max_rounds=50)
    assert again.session_id == s.session_id   # 表仍在、查询正常
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_sqlite_store.py -q`
Expected: FAIL（ModuleNotFoundError: ...sqlite_store）

- [ ] **Step 3: 写 SqliteSessionStore（参数化查询，表名常量）**

```python
# claw_engine/engine/persistence/sqlite_store.py
from __future__ import annotations
import sqlite3
import uuid
from claw_engine.engine.persistence.contracts import Session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    session_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    external_thread_key TEXT NOT NULL,
    backend_name TEXT NOT NULL,
    backend_thread_id TEXT,
    round_count INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL,
    last_active REAL NOT NULL DEFAULT 0,
    UNIQUE (workspace_id, channel, external_thread_key)
);
CREATE TABLE IF NOT EXISTS processed_message (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (session_id, message_id)
);
"""


class SqliteSessionStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"], workspace_id=row["workspace_id"],
            channel=row["channel"], external_thread_key=row["external_thread_key"],
            backend_name=row["backend_name"], backend_thread_id=row["backend_thread_id"],
            round_count=row["round_count"], max_rounds=row["max_rounds"],
            last_active=row["last_active"],
        )

    def get_or_create(self, *, workspace_id: str, channel: str, external_thread_key: str,
                      backend_name: str, max_rounds: int) -> Session:
        row = self._conn.execute(
            "SELECT * FROM session WHERE workspace_id=? AND channel=? AND external_thread_key=?",
            (workspace_id, channel, external_thread_key),
        ).fetchone()
        if row is not None:
            return self._row_to_session(row)
        session = Session(
            session_id=uuid.uuid4().hex, workspace_id=workspace_id, channel=channel,
            external_thread_key=external_thread_key, backend_name=backend_name,
            max_rounds=max_rounds,
        )
        self._conn.execute(
            "INSERT INTO session (session_id, workspace_id, channel, external_thread_key, "
            "backend_name, backend_thread_id, round_count, max_rounds, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session.session_id, session.workspace_id, session.channel, session.external_thread_key,
             session.backend_name, session.backend_thread_id, session.round_count,
             session.max_rounds, session.last_active),
        )
        self._conn.commit()
        return session

    def save(self, session: Session) -> None:
        self._conn.execute(
            "UPDATE session SET backend_thread_id=?, round_count=?, max_rounds=?, last_active=? "
            "WHERE session_id=?",
            (session.backend_thread_id, session.round_count, session.max_rounds,
             session.last_active, session.session_id),
        )
        self._conn.commit()

    def is_processed(self, session_id: str, message_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM processed_message WHERE session_id=? AND message_id=?",
            (session_id, message_id),
        ).fetchone() is not None

    def mark_processed(self, session_id: str, message_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_message (session_id, message_id) VALUES (?, ?)",
            (session_id, message_id),
        )
        self._conn.commit()
```

- [ ] **Step 4: sqlite 专属测试 PASS（2 passed）**

Run: `.venv/bin/pytest tests/test_sqlite_store.py -q`
Expected: 2 passed

- [ ] **Step 5: 把 sqlite 接入共享套件**

修改 `tests/contract/test_sessionstore_contract.py`：
```python
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
STORES = [
    ("memory", lambda: MemorySessionStore()),
    ("sqlite", lambda: SqliteSessionStore(":memory:")),
]
```

Run: `.venv/bin/pytest tests/contract/test_sessionstore_contract.py -q`
Expected: 8 passed（4 用例 × memory+sqlite，ids 含 memory 与 sqlite）

- [ ] **Step 6: 全量 + ruff + purity**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 全量 PASS；All checks passed；purity 2 passed

- [ ] **Step 7: Commit**

```bash
git add claw_engine/engine/persistence/sqlite_store.py tests/test_sqlite_store.py tests/contract/test_sessionstore_contract.py
git commit -m "feat: add SqliteSessionStore (parameterized, durable); wire into shared store suite"
```

---

### Task 4: ConversationService（L1 多轮续接 + 轮次 + 去重）

**Files:**
- Create: `claw_engine/engine/orchestration/conversation.py`
- Test: `tests/test_conversation_service.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_conversation_service.py
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.engine.orchestration.conversation import (
    ConversationService, SessionRoundsExceeded, DuplicateMessage,
)
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentErrorKind, AgentRunResult, TokenUsage,
    BackendCapabilities, BackendHealth,
)


class _SpyBackend:
    """记录每次 run 收到的 backend_thread_id；首轮返回固定 tid。"""
    name = "spy"

    def __init__(self) -> None:
        self.received: list = []
        self.calls = 0

    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))

    def healthcheck(self):
        return BackendHealth(ok=True)

    def run(self, req):
        self.calls += 1
        self.received.append(req.backend_thread_id)
        tid = req.backend_thread_id or "bt_1"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=f"echo: {req.prompt}")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id=tid,
                         result=AgentRunResult(backend_thread_id=tid, final_text=f"echo: {req.prompt}",
                                               usage=TokenUsage()))


class _FlakyBackend:
    """第一次返回 ERROR（run_turn 抛 AgentRunFailed），之后成功。"""
    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))

    def healthcheck(self):
        return BackendHealth(ok=True)

    def run(self, req):
        self.calls += 1
        if self.calls == 1:
            yield AgentEvent(kind=AgentEventKind.ERROR,
                             error=AgentError(kind=AgentErrorKind.BACKEND_CRASH, message="boom"))
            return
        tid = req.backend_thread_id or "bt_1"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text="ok")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id=tid,
                         result=AgentRunResult(backend_thread_id=tid, final_text="ok", usage=TokenUsage()))


def _service():
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    svc = ConversationService(Engine(reg), MemorySessionStore())
    return svc, spy

_KW = dict(workspace_id="w", channel="c", external_thread_key="t", cwd="/tmp", env={}, backend_name="spy")

def test_first_turn_runs_and_persists_backend_thread_id():
    svc, spy = _service()
    res = svc.handle(text="hi", **_KW)
    assert res.final_text == "echo: hi"
    assert spy.received == [None]            # 首轮无 resume 句柄

def test_second_turn_resumes_with_persisted_thread_id():
    svc, spy = _service()
    svc.handle(text="one", **_KW)
    svc.handle(text="two", **_KW)
    assert spy.received == [None, "bt_1"]    # 第二轮带上首轮回填的句柄 = 续接

def test_max_rounds_exceeded_raises():
    svc, spy = _service()
    svc.handle(text="a", max_rounds=1, **_KW)        # round_count -> 1
    with pytest.raises(SessionRoundsExceeded):
        svc.handle(text="b", max_rounds=1, **_KW)    # 已达上限，拒绝
    assert spy.calls == 1                            # 第二次未真正 run

def test_duplicate_message_id_skipped():
    svc, spy = _service()
    svc.handle(text="x", message_id="m1", **_KW)
    with pytest.raises(DuplicateMessage):
        svc.handle(text="x-again", message_id="m1", **_KW)
    assert spy.calls == 1                            # 重复消息未重复 run

def test_failed_turn_not_counted_and_message_retriable():
    flaky = _FlakyBackend()
    reg = EngineRegistry()
    reg.register_backend("flaky", lambda: flaky)
    store = MemorySessionStore()
    svc = ConversationService(Engine(reg), store)
    kw = dict(workspace_id="w", channel="c", external_thread_key="t",
              cwd="/tmp", env={}, backend_name="flaky")
    # 第一次：backend 返回 ERROR → run_turn 抛 AgentRunFailed
    with pytest.raises(AgentRunFailed):
        svc.handle(text="x", message_id="m1", **kw)
    session = store.get_or_create(workspace_id="w", channel="c", external_thread_key="t",
                                  backend_name="flaky", max_rounds=50)
    assert session.round_count == 0                          # 失败轮不计数
    assert not store.is_processed(session.session_id, "m1")  # 未标记 processed
    # 第二次同 message_id：应再次触发 backend（非 DuplicateMessage），证明可重试
    res = svc.handle(text="x", message_id="m1", **kw)
    assert res.final_text == "ok"
    assert flaky.calls == 2
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_conversation_service.py -q`
Expected: FAIL（ModuleNotFoundError: ...orchestration.conversation）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/orchestration/conversation.py
from __future__ import annotations
from typing import Mapping, Optional
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.persistence.contracts import SessionStore
from claw_engine.engine.runtime.contracts import AgentRunResult

DEFAULT_MAX_ROUNDS = 50


class SessionRoundsExceeded(RuntimeError):
    def __init__(self, session_id: str, max_rounds: int) -> None:
        super().__init__(f"session {session_id} 已达最大轮数 {max_rounds}")
        self.session_id = session_id
        self.max_rounds = max_rounds


class DuplicateMessage(RuntimeError):
    def __init__(self, session_id: str, message_id: str) -> None:
        super().__init__(f"message {message_id} 在 session {session_id} 已处理")
        self.session_id = session_id
        self.message_id = message_id


class ConversationService:
    """L1：把一条 (workspace_id, channel, external_thread_key) 上的消息接到有状态会话。"""

    def __init__(self, engine: Engine, store: SessionStore) -> None:
        self._engine = engine
        self._store = store

    def handle(self, *, workspace_id: str, channel: str, external_thread_key: str,
               text: str, cwd: str, env: Mapping[str, str], backend_name: str,
               max_rounds: int = DEFAULT_MAX_ROUNDS, message_id: Optional[str] = None,
               model: Optional[str] = None) -> AgentRunResult:
        session = self._store.get_or_create(
            workspace_id=workspace_id, channel=channel, external_thread_key=external_thread_key,
            backend_name=backend_name, max_rounds=max_rounds,
        )
        if message_id is not None and self._store.is_processed(session.session_id, message_id):
            raise DuplicateMessage(session.session_id, message_id)
        if session.round_count >= session.max_rounds:
            raise SessionRoundsExceeded(session.session_id, session.max_rounds)

        result = self._engine.run_turn(
            backend_name=session.backend_name, prompt=text, cwd=cwd, env=env,
            backend_thread_id=session.backend_thread_id, model=model,
            metadata={"workspace_id": workspace_id, "session_id": session.session_id,
                      "channel": channel},
        )

        self._store.save(session.with_turn(backend_thread_id=result.backend_thread_id))
        if message_id is not None:
            self._store.mark_processed(session.session_id, message_id)
        return result
```

> 说明：去重检查在轮次检查之前——重复消息即便会话已达上限也应被识别为重复而非轮次错误。`run_turn` 失败（抛 `AgentRunFailed`）时不落 `save`/`mark_processed`，该轮不计数、消息不标记已处理（允许重试），符合「失败不静默吞」。

- [ ] **Step 4: 跑确认 PASS（5 passed）**

Run: `.venv/bin/pytest tests/test_conversation_service.py -q`
Expected: 5 passed

- [ ] **Step 5: purity（ConversationService 在 engine/，只依赖 engine 抽象）+ 全量 + ruff**

Run: `.venv/bin/pytest tests/purity -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests`
Expected: purity 2 passed；全量 PASS；All checks passed

- [ ] **Step 6: Commit**

```bash
git add claw_engine/engine/orchestration/conversation.py tests/test_conversation_service.py
git commit -m "feat: add ConversationService (multi-turn resume, max_rounds, dedup)"
```

---

### Task 5: 端到端多轮续接（sqlite 落地）+ 最终回归

**Files:**
- Test: `tests/test_conversation_sqlite_e2e.py`

- [ ] **Step 1: 写端到端测试（ConversationService + SqliteSessionStore 跨"重启"续接）**

```python
# tests/test_conversation_sqlite_e2e.py
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.persistence.sqlite_store import SqliteSessionStore
from tests.contract.fake_backend import FakeBackend


def _service(db_path):
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    return ConversationService(Engine(reg), SqliteSessionStore(db_path))

_KW = dict(workspace_id="w", channel="c", external_thread_key="t", cwd="/tmp", env={}, backend_name="fake")

def test_multiturn_resume_persists_across_restart(tmp_path):
    db = str(tmp_path / "s.db")
    svc1 = _service(db)
    r1 = svc1.handle(text="hello", **_KW)
    assert r1.backend_thread_id == "fake-thread-1"

    # 新进程/新 store（同 db 文件）= 模拟重启；应续接同一 backend_thread_id
    svc2 = _service(db)
    store2 = SqliteSessionStore(db)
    session = store2.get_or_create(**_KW, max_rounds=50)
    assert session.backend_thread_id == "fake-thread-1"
    assert session.round_count == 1

    r2 = svc2.handle(text="again", **_KW)
    assert r2.backend_thread_id == "fake-thread-1"   # 续接，非新会话
    session2 = SqliteSessionStore(db).get_or_create(**_KW, max_rounds=50)
    assert session2.round_count == 2
```

> 注：`FakeBackend`（`tests/contract/fake_backend.py`）首轮无句柄返回 `fake-thread-1`，续接时回显传入句柄——正好验证「persist→resume」闭环。

- [ ] **Step 2: 跑确认 PASS**

Run: `.venv/bin/pytest tests/test_conversation_sqlite_e2e.py -q`
Expected: PASS（1 passed）

- [ ] **Step 3: 最终全量 + ruff + purity（P3 验收）**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 全量 PASS；All checks passed；purity 2 passed（engine 仍零 CLI 字面量；persistence/orchestration 均无业务/CLI 词）

- [ ] **Step 4: Commit**

```bash
git add tests/test_conversation_sqlite_e2e.py
git commit -m "test: end-to-end multi-turn resume over SqliteSessionStore"
```

---

## Self-Review

**1. 需求/决策覆盖：**
- 决策1（ConversationService 包 Engine+Store，run_turn 无状态）→ Task 4 ✅
- 决策2（max_rounds 超限抛 `SessionRoundsExceeded`）→ Task 4 `test_max_rounds_exceeded_raises` ✅
- 决策3（去重纳入 P3，`DuplicateMessage`）→ Task 4 `test_duplicate_message_id_skipped` + 共享套件 dedup ✅
- 失败语义（run_turn 失败 → 不计轮/不标 processed/可重试）→ Task 4 `test_failed_turn_not_counted_and_message_retriable` ✅
- spec §3.4 Session 入口 vs backend resume 句柄分离 → `Session` 模型（session_id PK / 唯一键三元组 / nullable backend_thread_id）Task 1/3 ✅
- spec §5 SessionStore 抽象 memory/sqlite 可换 → Task 2/3 + 共享 store 契约套件 ✅
- spec §6.3 安全（参数化查询、表名常量）→ Task 3 `test_sqlite_uses_parameterized_queries` ✅
- 多轮续接（persist→resume 闭环）→ Task 4 `test_second_turn_resumes...` + Task 5 sqlite e2e ✅
- 边界：不解析 workspace→cwd/env、不接 channel、不改 CLI → 全程 cwd/env 入参透传，CLI 不动 ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码。✅

**3. 类型/签名一致性：** `Session`(session_id/workspace_id/channel/external_thread_key/backend_name/backend_thread_id/round_count/max_rounds/last_active) + `with_turn(backend_thread_id,now)`；`SessionStore`(get_or_create/save/is_processed/mark_processed)；`ConversationService.handle(workspace_id,channel,external_thread_key,text,cwd,env,backend_name,max_rounds,message_id,model)`；`SessionRoundsExceeded`/`DuplicateMessage` 在各 task 间一致；run_turn 调用签名与 P1 Engine 一致。✅

**已知取舍（实现注意）：**
- `get_or_create` 对已存在会话忽略新传入 backend_name/max_rounds（existing wins）——切 backend/改上限需换 external_thread_key 开新会话（spec §3.4 一致）。
- 去重检查先于轮次检查（重复消息优先识别为重复）。
- run_turn 失败时不 save/不 mark_processed（该轮不计数、可重试）。
- CLI 暂不接 ConversationService（多轮入口属 channel/P6）；P3 仅由测试驱动 L1。
- sqlite 每次操作即时 commit；连接非线程安全——多线程并发属后续（生产 mysql adapter / 连接管理）课题。
