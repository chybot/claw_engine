# claw_engine P6a — MessagingGateway contract + 最小 channel 闭环 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 定下 L0 `MessagingGateway` contract（入站校验/归一 + 出站回复/进度）与精简 `IncomingMessage`，实现一个最小 **WebhookChannel** 适配器（HMAC 校验、解析、回复捕获），并用 `ChannelRunner` + 占位路由串通 `raw → verify → parse → route → WorkspaceConversationGateway → reply` 最小闭环。

**Architecture:** `engine/channels/` 放 contract（`MessagingGateway`/`IncomingMessage`/`ReplyTarget`/`WorkspaceRouter`/`InboundAuthError` + `ChannelRunner` + 占位 `StaticWorkspaceRouter`，业务无关）。具体 `WebhookChannel` 在 `adapters/channels/`。ChannelRunner 复用 P5 的 `WorkspaceConversationGateway`。

**Tech Stack:** Python 3.11+，标准库 `hmac`/`hashlib`/`json`，pytest，ruff。全 hermetic（payload 进 / reply 捕获，不起真实 HTTP server）。

**前置参考：** spec §4.4（seam① MessagingGateway）、§6.3（webhook 校验）、P5 的 `WorkspaceConversationGateway`、`tests/contract/fake_backend.py`。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策
1. **IncomingMessage 精简**：`{channel, raw_user_ref, external_thread_key, text, message_id, attachments, is_command}`。`raw_user_ref` 是渠道原始身份，**待 P6b IdentityProvider 解析**。
2. **P6a 串最小闭环 + 占位路由**：`ChannelRunner` 用占位 `StaticWorkspaceRouter`（固定 workspace_id）接到 `WorkspaceConversationGateway`；P6b 用 IdentityProvider-based 路由替换。
3. **一个最小 WebhookChannel**（hermetic）：`verify_inbound` 做 HMAC-SHA256 签名校验（安全相关入口）、`parse_inbound` 归一、`send_text/attachments` 捕获、progress no-op。
4. **contract 含 progress**：`MessagingGateway` Protocol 完整含 `start_progress/update_progress`，最小 channel 可 no-op；真正用在 P6d。

### 边界（P6a 不做）
- **不**做真实身份解析/RBAC（P6b）；`raw_user_ref` 仅透传，占位路由不看它。
- **不**接 langfuse/redaction 强制（P6c）。
- **不**做 skill provisioning / run_workflow tool bridge（P6d）。
- **不**起真实 HTTP server（adapters 后续可加 FastAPI 入口）；P6a 的 channel 是 payload-in/reply-capture 的纯逻辑。
- **不**改 P1–P5 已落地代码（仅新增；ChannelRunner 复用 WorkspaceConversationGateway）。

### 入站闭环（ChannelRunner.handle_raw）
```
raw(str body) + headers
  → gateway.verify_inbound(raw, headers)        # 失败抛 InboundAuthError，阻断
  → msg = gateway.parse_inbound(raw)            # -> IncomingMessage
  → workspace_id = router.route(msg)            # 占位：固定 ws
  → result = conversation_gateway.handle(workspace_id, channel, external_thread_key,
                                         text, backend_name=default, message_id)
  → gateway.send_text(ReplyTarget(...), result.final_text)
  → return result
```
verify 失败时**不** parse/route/run/send（安全：未通过校验不进引擎）。

---

### Task 1: Channel 契约类型

**Files:**
- Create: `claw_engine/engine/channels/__init__.py`
- Create: `claw_engine/engine/channels/contracts.py`
- Test: `tests/test_channel_contracts.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_channel_contracts.py
import dataclasses
import pytest
from claw_engine.engine.channels.contracts import (
    IncomingMessage, ReplyTarget, ProgressState, InboundAuthError,
)

def test_incoming_message_frozen_and_defaults():
    m = IncomingMessage(channel="webhook", raw_user_ref="u1",
                        external_thread_key="t1", text="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.text = "x"
    assert m.message_id is None
    assert m.attachments == ()
    assert m.is_command is False

def test_reply_target_frozen_eq():
    a = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    b = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    assert a == b

def test_progress_state_defaults():
    s = ProgressState(message="working")
    assert s.fraction is None

def test_inbound_auth_error_is_exception():
    assert issubclass(InboundAuthError, Exception)
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_channel_contracts.py -q`
Expected: FAIL（ModuleNotFoundError: ...channels.contracts）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/channels/contracts.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, runtime_checkable


class InboundAuthError(Exception):
    """入站校验失败（签名/token）。未通过则不进引擎。"""


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    raw_user_ref: str                       # 渠道原始身份，待 P6b IdentityProvider 解析
    external_thread_key: str
    text: str
    message_id: Optional[str] = None
    attachments: tuple[str, ...] = ()
    is_command: bool = False


@dataclass(frozen=True)
class ReplyTarget:
    channel: str
    external_thread_key: str
    raw_user_ref: str


@dataclass(frozen=True)
class ProgressState:
    message: str
    fraction: Optional[float] = None


# 进度句柄对引擎不透明（channel 自定义）
ProgressHandle = str


@runtime_checkable
class MessagingGateway(Protocol):
    def verify_inbound(self, raw: str, headers: Mapping[str, str]) -> None: ...  # 失败抛 InboundAuthError
    def parse_inbound(self, raw: str) -> IncomingMessage: ...
    def send_text(self, target: ReplyTarget, text: str) -> None: ...
    def send_attachments(self, target: ReplyTarget, files: tuple[str, ...]) -> None: ...
    def start_progress(self, target: ReplyTarget, steps: tuple[str, ...]) -> ProgressHandle: ...
    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None: ...


@runtime_checkable
class WorkspaceRouter(Protocol):
    def route(self, message: IncomingMessage) -> str:
        """IncomingMessage -> workspace_id。P6a 占位实现固定返回；P6b 用 IdentityProvider 解析。"""
        ...
```

- [ ] **Step 4: 跑确认 PASS（4 passed）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_channel_contracts.py -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 4 passed；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/channels/__init__.py claw_engine/engine/channels/contracts.py tests/test_channel_contracts.py
git commit -m "feat: add channel contracts (MessagingGateway, IncomingMessage, WorkspaceRouter)"
```

---

### Task 2: WebhookChannel 适配器（HMAC 校验 + 解析 + 捕获）

**Files:**
- Create: `claw_engine/adapters/channels/__init__.py`
- Create: `claw_engine/adapters/channels/webhook.py`
- Test: `tests/test_webhook_channel.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_webhook_channel.py
import hashlib
import hmac
import json
import pytest
from claw_engine.engine.channels.contracts import InboundAuthError, ReplyTarget, ProgressState
from claw_engine.adapters.channels.webhook import WebhookChannel

SECRET = "s3cr3t"

def _sig(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()

def test_verify_inbound_accepts_valid_signature():
    ch = WebhookChannel(SECRET)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "hi"})
    ch.verify_inbound(body, {"X-Signature": _sig(body)})   # 不抛即通过
    ch.verify_inbound(body, {"x-signature": _sig(body)})   # header 名大小写不敏感

def test_verify_inbound_rejects_missing_and_wrong_signature():
    ch = WebhookChannel(SECRET)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "hi"})
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {})                         # 缺签名
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {"X-Signature": "deadbeef"})  # 错签名

def test_parse_inbound_maps_fields_and_command_flag():
    ch = WebhookChannel(SECRET)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "/help",
                       "message_id": "m1", "attachments": ["/tmp/a.png"]})
    msg = ch.parse_inbound(body)
    assert msg.channel == "webhook"
    assert msg.raw_user_ref == "u1" and msg.external_thread_key == "t1"
    assert msg.text == "/help" and msg.message_id == "m1"
    assert msg.attachments == ("/tmp/a.png",)
    assert msg.is_command is True
    # 非 slash 文本
    msg2 = ch.parse_inbound(json.dumps({"user": "u1", "thread": "t1", "text": "hi"}))
    assert msg2.is_command is False

def test_parse_inbound_malformed_raises_value_error():
    ch = WebhookChannel(SECRET)
    with pytest.raises(ValueError):
        ch.parse_inbound(json.dumps({"user": "u1"}))        # 缺 thread/text

def test_parse_inbound_rejects_non_list_attachments():
    ch = WebhookChannel(SECRET)
    with pytest.raises(ValueError):
        ch.parse_inbound(json.dumps({"user": "u", "thread": "t", "text": "hi",
                                     "attachments": "bad"}))   # 字符串非法，防被 tuple 成字符序列

def test_send_text_and_attachments_captured():
    ch = WebhookChannel(SECRET)
    target = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    ch.send_text(target, "hello")
    ch.send_attachments(target, ("/tmp/a.png",))
    assert ch.sent_texts == [(target, "hello")]
    assert ch.sent_attachments == [(target, ("/tmp/a.png",))]

def test_progress_is_recorded_and_returns_handle():
    ch = WebhookChannel(SECRET)
    target = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    handle = ch.start_progress(target, ("step1", "step2"))
    assert isinstance(handle, str)
    state = ProgressState(message="working", fraction=0.5)
    ch.update_progress(handle, state)
    assert ch.progress_started == [(target, ("step1", "step2"), handle)]   # 记录 start
    assert ch.progress_updates == [(handle, state)]                        # 记录 update
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_webhook_channel.py -q`
Expected: FAIL（ModuleNotFoundError: ...adapters.channels.webhook）

- [ ] **Step 3: 写实现**

```python
# claw_engine/adapters/channels/webhook.py
from __future__ import annotations
import hashlib
import hmac
import json
from typing import List, Mapping, Tuple
from claw_engine.engine.channels.contracts import (
    IncomingMessage, InboundAuthError, ProgressHandle, ProgressState, ReplyTarget,
)


class WebhookChannel:
    """最小 webhook 风格 channel：HMAC-SHA256 校验 + JSON 解析 + 回复捕获（hermetic，不起 HTTP server）。"""

    name = "webhook"

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.sent_texts: List[Tuple[ReplyTarget, str]] = []
        self.sent_attachments: List[Tuple[ReplyTarget, Tuple[str, ...]]] = []
        self.progress_started: List[Tuple[ReplyTarget, Tuple[str, ...], str]] = []
        self.progress_updates: List[Tuple[str, ProgressState]] = []

    def verify_inbound(self, raw: str, headers: Mapping[str, str]) -> None:
        lower = {k.lower(): v for k, v in headers.items()}   # HTTP header 名大小写不敏感
        sig = lower.get("x-signature")
        if not sig:
            raise InboundAuthError("missing X-Signature")
        expected = hmac.new(self._secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise InboundAuthError("signature mismatch")

    def parse_inbound(self, raw: str) -> IncomingMessage:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed webhook body: {exc}") from exc
        try:
            user = payload["user"]
            thread = payload["thread"]
            text = payload["text"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"webhook payload 缺少必需字段: {exc}") from exc
        raw_attachments = payload.get("attachments", []) or []
        if not isinstance(raw_attachments, (list, tuple)):   # 防字符串被 tuple 成字符序列
            raise ValueError(f"attachments 必须是 list/tuple，得到 {type(raw_attachments).__name__}")
        return IncomingMessage(
            channel=self.name, raw_user_ref=str(user), external_thread_key=str(thread),
            text=str(text), message_id=payload.get("message_id"),
            attachments=tuple(str(a) for a in raw_attachments),
            is_command=str(text).startswith("/"),
        )

    def send_text(self, target: ReplyTarget, text: str) -> None:
        self.sent_texts.append((target, text))

    def send_attachments(self, target: ReplyTarget, files: Tuple[str, ...]) -> None:
        self.sent_attachments.append((target, tuple(files)))

    def start_progress(self, target: ReplyTarget, steps: Tuple[str, ...]) -> ProgressHandle:
        handle = f"prog-{target.external_thread_key}"
        self.progress_started.append((target, tuple(steps), handle))
        return handle

    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None:
        self.progress_updates.append((handle, state))   # P6a 仅记录；P6d 真正渲染
```

- [ ] **Step 4: 跑确认 PASS（6 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_webhook_channel.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 7 passed；全量 PASS；purity 2 passed（webhook 在 adapters，engine 仍纯净）；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/adapters/channels/__init__.py claw_engine/adapters/channels/webhook.py tests/test_webhook_channel.py
git commit -m "feat: add WebhookChannel adapter (HMAC verify, parse, capture, progress no-op)"
```

---

### Task 3: StaticWorkspaceRouter + ChannelRunner + 入站闭环 e2e

**Files:**
- Create: `claw_engine/engine/channels/routing.py`
- Create: `claw_engine/engine/channels/runner.py`
- Test: `tests/test_channel_runner_e2e.py`

- [ ] **Step 1: 写失败测试（raw → reply 全链路）**

```python
# tests/test_channel_runner_e2e.py
import hashlib
import hmac
import json
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.channels.routing import StaticWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.engine.channels.contracts import ReplyTarget
from claw_engine.adapters.channels.webhook import WebhookChannel
from tests.contract.fake_backend import FakeBackend

SECRET = "s3cr3t"

def _sig(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()

def _runner(channel):
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: FakeBackend())
    convo = ConversationService(Engine(reg), MemorySessionStore())
    resolver = WorkspaceResolver(LayeredConfigProvider(), InMemorySecretProvider(),
                                 workspaces_root="/srv/ws")
    ws_gateway = WorkspaceConversationGateway(resolver, convo)
    router = StaticWorkspaceRouter("ws1")
    return ChannelRunner(channel, router, ws_gateway, default_backend_name="fake")

def test_raw_to_reply_loop():
    channel = WebhookChannel(SECRET)
    runner = _runner(channel)
    body = json.dumps({"user": "u1", "thread": "t1", "text": "hello", "message_id": "m1"})
    result = runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert result.final_text == "echo: hello"
    target = ReplyTarget(channel="webhook", external_thread_key="t1", raw_user_ref="u1")
    assert channel.sent_texts == [(target, "echo: hello")]

def test_static_router_returns_fixed_workspace():
    from claw_engine.engine.channels.contracts import IncomingMessage
    r = StaticWorkspaceRouter("wsX")
    msg = IncomingMessage(channel="webhook", raw_user_ref="u", external_thread_key="t", text="hi")
    assert r.route(msg) == "wsX"
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_channel_runner_e2e.py -q`
Expected: FAIL（ModuleNotFoundError: ...channels.routing / runner）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/channels/routing.py
from __future__ import annotations
from claw_engine.engine.channels.contracts import IncomingMessage


class StaticWorkspaceRouter:
    """P6a 占位路由：任何消息固定路由到一个 workspace。P6b 用 IdentityProvider-based 路由替换。"""

    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id

    def route(self, message: IncomingMessage) -> str:
        return self._workspace_id
```

```python
# claw_engine/engine/channels/runner.py
from __future__ import annotations
from typing import Mapping, Optional
from claw_engine.engine.channels.contracts import MessagingGateway, ReplyTarget, WorkspaceRouter
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.runtime.contracts import AgentRunResult


class ChannelRunner:
    """串通入站闭环：raw → verify → parse → route → WorkspaceConversationGateway → send_text。"""

    def __init__(self, gateway: MessagingGateway, router: WorkspaceRouter,
                 conversation: WorkspaceConversationGateway, *, default_backend_name: str) -> None:
        self._gateway = gateway
        self._router = router
        self._conversation = conversation
        self._default_backend = default_backend_name

    def handle_raw(self, raw: str, headers: Mapping[str, str]) -> AgentRunResult:
        self._gateway.verify_inbound(raw, headers)        # 失败抛 InboundAuthError，阻断后续
        msg = self._gateway.parse_inbound(raw)
        workspace_id = self._router.route(msg)
        result = self._conversation.handle(
            workspace_id=workspace_id, channel=msg.channel,
            external_thread_key=msg.external_thread_key, text=msg.text,
            backend_name=self._default_backend, message_id=msg.message_id,
        )
        target = ReplyTarget(channel=msg.channel, external_thread_key=msg.external_thread_key,
                             raw_user_ref=msg.raw_user_ref)
        self._gateway.send_text(target, result.final_text)
        return result
```

- [ ] **Step 4: 跑确认 PASS（2 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_channel_runner_e2e.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 2 passed；全量 PASS；purity 2 passed（channels 在 engine 但无业务/CLI 字面量）；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/channels/routing.py claw_engine/engine/channels/runner.py tests/test_channel_runner_e2e.py
git commit -m "feat: add StaticWorkspaceRouter + ChannelRunner (inbound->reply loop)"
```

---

### Task 4: 安全 e2e（校验失败阻断闭环）+ 最终回归

**Files:**
- Test: `tests/test_channel_runner_security.py`

- [ ] **Step 1: 写测试（坏签名 → InboundAuthError，backend 不跑、不回复）**

```python
# tests/test_channel_runner_security.py
import json
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.channels.routing import StaticWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.engine.channels.contracts import InboundAuthError
from claw_engine.adapters.channels.webhook import WebhookChannel
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage, BackendCapabilities, BackendHealth,
)

SECRET = "s3cr3t"


class _SpyBackend:
    name = "spy"
    def __init__(self):
        self.req = None
    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))
    def healthcheck(self):
        return BackendHealth(ok=True)
    def run(self, req):
        self.req = req
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt",
                         result=AgentRunResult(backend_thread_id="bt", final_text="ok", usage=TokenUsage()))


def test_bad_signature_blocks_loop_no_backend_no_reply():
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    resolver = WorkspaceResolver(LayeredConfigProvider(), InMemorySecretProvider(),
                                 workspaces_root="/srv/ws")
    ws_gateway = WorkspaceConversationGateway(resolver, convo)
    channel = WebhookChannel(SECRET)
    runner = ChannelRunner(channel, StaticWorkspaceRouter("ws1"), ws_gateway,
                           default_backend_name="spy")

    body = json.dumps({"user": "u1", "thread": "t1", "text": "hello"})
    with pytest.raises(InboundAuthError):
        runner.handle_raw(body, {"X-Signature": "WRONG"})

    assert spy.req is None              # 未通过校验 → backend 从未运行
    assert channel.sent_texts == []     # 没有任何回复发出
```

- [ ] **Step 2: 跑确认 PASS（1 passed）**

Run: `.venv/bin/pytest tests/test_channel_runner_security.py -q`
Expected: 1 passed（实现已在 T3；本测试锁定「校验失败不进引擎」安全语义。若 FAIL 回 T3 修 runner 顺序）

- [ ] **Step 3: 最终全量 + ruff + purity（P6a 验收）**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 全量 PASS；All checks passed；purity 2 passed（engine/channels 无业务/CLI 字面量；webhook 在 adapters）

- [ ] **Step 4: Commit**

```bash
git add tests/test_channel_runner_security.py
git commit -m "test: lock inbound-auth-failure blocks engine loop (no backend, no reply)"
```

---

## Self-Review

**1. 决策/边界覆盖：**
- 决策1（IncomingMessage 精简）→ Task 1 `IncomingMessage` 7 字段 ✅
- 决策2（最小闭环 + 占位路由）→ Task 3 `StaticWorkspaceRouter` + `ChannelRunner` + raw→reply e2e ✅
- 决策3（WebhookChannel + verify_inbound HMAC）→ Task 2 `test_verify_inbound_*` ✅
- 决策4（contract 含 progress，channel no-op）→ Task 1 Protocol 含 start/update_progress + Task 2 `test_progress_is_noop_but_returns_handle` ✅
- 安全：校验失败阻断闭环（未通过不进引擎）→ Task 4 `test_bad_signature_blocks_loop_no_backend_no_reply` ✅
- 复用 P5 `WorkspaceConversationGateway`、不改 P1–P5 → ChannelRunner 仅组合 ✅
- 加固A：`contracts.py` 去未用 `field` 导入（F401）✅
- 加固B：`verify_inbound` header 名大小写不敏感 → Task 2 `test_verify_inbound_accepts_valid_signature` 含小写断言 ✅
- 加固C：`parse_inbound` attachments 必须 list/tuple（防字符串被 tuple 成字符序列）→ Task 2 `test_parse_inbound_rejects_non_list_attachments` ✅
- 加固D：progress 记录 `progress_started`/`progress_updates`（为 P6d 接得上）→ Task 2 `test_progress_is_recorded_and_returns_handle` ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码（测试片段避免 `a; b` 复合语句以过 ruff E702）。✅

**3. 类型/签名一致性：** `IncomingMessage`(channel/raw_user_ref/external_thread_key/text/message_id/attachments/is_command)；`ReplyTarget`(channel/external_thread_key/raw_user_ref)；`ProgressState`(message/fraction)；`MessagingGateway`(verify_inbound/parse_inbound/send_text/send_attachments/start_progress/update_progress)；`WorkspaceRouter.route(message)->str`；`WebhookChannel(secret)` 实现全部方法；`StaticWorkspaceRouter(workspace_id)`；`ChannelRunner(gateway,router,conversation,default_backend_name).handle_raw(raw,headers)`；调用 `WorkspaceConversationGateway.handle(...)` 与 P5 签名一致。各 task 间一致。✅

**已知取舍（实现注意）：**
- `raw` 约定为请求体字符串（verify_inbound 用它算 HMAC，parse_inbound 用它 json.loads）；真实 HTTP 入口（FastAPI 等）属 adapters 后续，P6a 不起 server。
- 占位 `StaticWorkspaceRouter` 固定 workspace、不看 `raw_user_ref`；P6b 用 IdentityProvider 解析身份→授权 workspace 替换。
- WebhookChannel 的 progress 仅记录不渲染（no-op）；真正进度推送在 P6d。
- ChannelRunner 的 `default_backend_name` 是 fallback；workspace spec 指定的 backend 仍优先（P5 gateway 行为）。
- redaction（P6c）尚未强制：P6a 不打日志/不接 trace，故无明文 secret 外泄面；接 langfuse 时（P6c）必须走 `redacted_env()`。
