# claw_engine P6b — IdentityProvider + workspace RBAC + user 层 config 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 把 P6a 的占位 `StaticWorkspaceRouter` 升级为「解析 `raw_user_ref` → User → 校验授权 workspace → 路由」的真实身份路由；补上 P5 预留的 user 层 config；workspace 授权失败抛 `WorkspaceAccessDenied`。

**Architecture:** `engine/identity/`（`IdentityProvider` Protocol + `User` + `InMemoryIdentityProvider` + 异常）。`WorkspaceRouter.route` 升级返回 `RouteDecision{workspace_id, user_id}`（加 `user_id`，使其能穿到 resolver）。`WorkspaceResolver` 加 user 层（`resolve(workspace_id, user_id=None)` + `UserConfigProvider`，precedence: global→workspace→user→secret，一处统管）。`IdentityWorkspaceRouter` 接 ChannelRunner。真实身份后端(SSO/LDAP)在 adapters 实现同 Protocol。

**Tech Stack:** Python 3.11+，标准库，pytest，ruff。全 hermetic。

**前置参考：** spec §3.4/§4.4(seam⑤)/§5/§6.3，P5（WorkspaceResolver/WorkspaceConversationGateway）、P6a（WorkspaceRouter/StaticWorkspaceRouter/ChannelRunner）。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策 + 一处模型协调
1. **Identity 模型**：`User{user_id, display_name, roles, default_workspace}`（`default_workspace` 为协调 Q4 路由 preview 而补：用户当前/默认 workspace）。`IdentityProvider = resolve_user(raw_user_ref)->User + authorized_workspaces(user)->tuple + can_access_workspace(user,ws)->bool`。`can_use_skill` 留 P6d。
2. **授权失败** → 抛 `WorkspaceAccessDenied(user_id, workspace_id)`（与 InboundAuthError/SessionRoundsExceeded 一致风格，不进引擎）。
3. **user config 覆盖放进 `WorkspaceResolver`**：`resolve(workspace_id, user_id=None)` + 注入 `UserConfigProvider`；env precedence(低→高) **global → workspace → user → secret**，一处统管。
4. **router 先解析成 User 再路由**：`IdentityWorkspaceRouter` 调 `resolve_user → 校验 default_workspace ∈ authorized → RouteDecision{workspace_id, user_id}`。

### 改动面（向后兼容优先）
- **加法式**（不破坏 P5 调用）：`WorkspaceResolver.resolve(workspace_id, user_id=None)`、`WorkspaceConversationGateway.handle(..., user_id=None)`、`WorkspaceResolver.__init__(..., user_config=None)`。P5 既有测试不传 user_id，保持绿。
- **契约升级**（P6a，需同步更新调用方与受影响测试）：`WorkspaceRouter.route(msg) -> RouteDecision`（原返回 str）；`StaticWorkspaceRouter`、`ChannelRunner` 随之更新；P6a 的 `test_static_router_returns_fixed_workspace` 改为断言 `.workspace_id`。

### 边界（P6b 不做）
- 不做 `can_use_skill` / skill provisioning（P6d）；不做 langfuse/redaction 强制（P6c）；不接真实 SSO/LDAP（adapters 后续）；不做 workspace 切换命令（`/onboard -space` 等属 channel 命令，后续）。default_workspace 由 IdentityProvider 给定。

### 入站闭环（升级后）
```
raw → verify_inbound → parse_inbound -> IncomingMessage
  → IdentityWorkspaceRouter.route(msg):
        user = identity.resolve_user(raw_user_ref)     # 未知 -> UnknownUser
        ws = user.default_workspace
        if ws is None or not can_access_workspace(user, ws): raise WorkspaceAccessDenied
        return RouteDecision(workspace_id=ws, user_id=user.user_id)
  → WorkspaceConversationGateway.handle(workspace_id, user_id, ...)
        → WorkspaceResolver.resolve(workspace_id, user_id)   # 含 user 层 env
  → send_text(reply)
```

---

### Task 1: IdentityProvider + User + 异常

**Files:**
- Create: `claw_engine/engine/identity/__init__.py`
- Create: `claw_engine/engine/identity/contracts.py`
- Create: `claw_engine/engine/identity/memory.py`
- Test: `tests/test_identity_provider.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_identity_provider.py
import pytest
from claw_engine.engine.identity.contracts import User, UnknownUser, WorkspaceAccessDenied
from claw_engine.engine.identity.memory import InMemoryIdentityProvider

def _provider():
    users = {"ref-u1": User(user_id="u1", display_name="Alice", roles=("dev",),
                            default_workspace="ws1")}
    authorized = {"u1": ("ws1", "ws2")}
    return InMemoryIdentityProvider(users=users, authorized=authorized)

def test_resolve_user_known():
    u = _provider().resolve_user("ref-u1")
    assert u.user_id == "u1" and u.default_workspace == "ws1" and u.roles == ("dev",)

def test_resolve_user_unknown_raises():
    with pytest.raises(UnknownUser) as ei:
        _provider().resolve_user("ghost")
    assert ei.value.raw_user_ref == "ghost"

def test_authorized_workspaces_and_can_access():
    p = _provider()
    u = p.resolve_user("ref-u1")
    assert p.authorized_workspaces(u) == ("ws1", "ws2")
    assert p.can_access_workspace(u, "ws1") is True
    assert p.can_access_workspace(u, "ws9") is False

def test_workspace_access_denied_carries_context():
    e = WorkspaceAccessDenied("u1", "ws9")
    assert e.user_id == "u1" and e.workspace_id == "ws9"
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_identity_provider.py -q`
Expected: FAIL（ModuleNotFoundError: ...identity.contracts）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/identity/contracts.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


class UnknownUser(KeyError):
    def __init__(self, raw_user_ref: str) -> None:
        super().__init__(raw_user_ref)
        self.raw_user_ref = raw_user_ref


class WorkspaceAccessDenied(PermissionError):
    def __init__(self, user_id: str, workspace_id: str) -> None:
        super().__init__(f"user {user_id} 无权访问 workspace {workspace_id}")
        self.user_id = user_id
        self.workspace_id = workspace_id


class WorkspaceRouteError(RuntimeError):
    """无法路由到 workspace（如用户无 default_workspace）。区别于越权 WorkspaceAccessDenied。"""


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str
    roles: tuple[str, ...] = ()
    default_workspace: Optional[str] = None


@runtime_checkable
class IdentityProvider(Protocol):
    def resolve_user(self, raw_user_ref: str) -> User: ...               # 未知抛 UnknownUser
    def authorized_workspaces(self, user: User) -> tuple[str, ...]: ...
    def can_access_workspace(self, user: User, workspace_id: str) -> bool: ...
```

```python
# claw_engine/engine/identity/memory.py
from __future__ import annotations
from typing import Mapping, Optional
from claw_engine.engine.identity.contracts import User, UnknownUser


class InMemoryIdentityProvider:
    """V1 内存身份 + 授权。真实后端(SSO/LDAP)在 adapters 实现同一 Protocol。"""

    def __init__(self, users: Optional[Mapping[str, User]] = None,
                 authorized: Optional[Mapping[str, tuple[str, ...]]] = None) -> None:
        self._users = dict(users or {})                      # key = raw_user_ref
        self._authorized = {k: tuple(v) for k, v in (authorized or {}).items()}  # key = user_id

    def resolve_user(self, raw_user_ref: str) -> User:
        try:
            return self._users[raw_user_ref]
        except KeyError:
            raise UnknownUser(raw_user_ref) from None

    def authorized_workspaces(self, user: User) -> tuple[str, ...]:
        return self._authorized.get(user.user_id, ())

    def can_access_workspace(self, user: User, workspace_id: str) -> bool:
        return workspace_id in self.authorized_workspaces(user)
```

- [ ] **Step 4: PASS（4 passed）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_identity_provider.py -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 4 passed；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/identity/__init__.py claw_engine/engine/identity/contracts.py claw_engine/engine/identity/memory.py tests/test_identity_provider.py
git commit -m "feat: add IdentityProvider, User, WorkspaceAccessDenied"
```

---

### Task 2: UserConfigProvider + WorkspaceResolver user 层

**Files:**
- Create: `claw_engine/engine/context/user_config.py`
- Modify: `claw_engine/engine/context/workspace.py`（resolve 加 user_id + user 层）
- Test: `tests/test_user_config_layer.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_user_config_layer.py
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver

def _resolver():
    config = LayeredConfigProvider(global_env={"REGION": "global", "TIER": "g"},
                                   workspace_env={"ws1": {"REGION": "id"}})
    secrets = InMemorySecretProvider({"ws1": {"TOKEN": "t-1", "TIER": "secret"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"TIER": "user", "USER_ONLY": "x"}}})
    return WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", user_config=user_config)

def test_user_layer_overrides_workspace_but_secret_wins():
    r = _resolver().resolve("ws1", user_id="u1")
    assert r.env["REGION"] == "id"            # workspace 覆盖 global
    assert r.env["USER_ONLY"] == "x"          # user 独有
    # TIER: global=g -> (workspace 无) -> user=user -> secret=secret；secret 最高
    assert r.env["TIER"] == "secret"
    assert r.env["TOKEN"] == "t-1"

def test_no_user_id_skips_user_layer():
    r = _resolver().resolve("ws1")            # 不传 user_id -> 无 user 层（向后兼容）
    assert "USER_ONLY" not in r.env
    assert r.env["TIER"] == "secret"          # secret 仍最高

def test_user_layer_between_workspace_and_secret():
    # 构造一个无 secret 冲突的 key 验证 user 覆盖 workspace
    config = LayeredConfigProvider(workspace_env={"ws1": {"K": "workspace"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"K": "user"}}})
    r = WorkspaceResolver(config, InMemorySecretProvider(), workspaces_root="/srv/ws",
                          user_config=user_config)
    assert r.resolve("ws1", user_id="u1").env["K"] == "user"   # user 覆盖 workspace
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_user_config_layer.py -q`
Expected: FAIL（ModuleNotFoundError: ...user_config / resolve 不接 user_id）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/context/user_config.py
from __future__ import annotations
from typing import Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class UserConfigProvider(Protocol):
    def get_env(self, user_id: str, workspace_id: str) -> Mapping[str, str]: ...


class InMemoryUserConfigProvider:
    """user_env: user_id -> workspace_id -> env。按 (user, workspace) 维度，避免跨 workspace 污染。"""

    def __init__(self,
                 user_env: Optional[Mapping[str, Mapping[str, Mapping[str, str]]]] = None) -> None:
        self._user_env = {
            uid: {wid: dict(env) for wid, env in by_ws.items()}
            for uid, by_ws in (user_env or {}).items()
        }

    def get_env(self, user_id: str, workspace_id: str) -> Mapping[str, str]:
        return dict(self._user_env.get(user_id, {}).get(workspace_id, {}))
```

`workspace.py` 改动（`WorkspaceResolver`）：
- `__init__` 增加 `user_config: Optional[UserConfigProvider] = None` 参数并保存（import `UserConfigProvider`）。
- `resolve` 签名改为 `resolve(self, workspace_id: str, user_id: Optional[str] = None) -> ResolvedWorkspace`，env 合并改为：
```python
        _validate_workspace_id(workspace_id)
        base_env = dict(self._config.get_env(workspace_id))          # global + workspace
        if user_id is not None and self._user_config is not None:
            base_env.update(self._user_config.get_env(user_id, workspace_id))  # user×workspace 覆盖
        secret_env = dict(self._secrets.get_secrets(workspace_id))
        env = {**base_env, **secret_env}                             # secret 最高
```
其余（cwd/sensitive_keys/spec/__repr__）不变。

- [ ] **Step 4: PASS（3 passed）+ 全量（P5 resolver 测试不回归）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_user_config_layer.py tests/test_workspace_resolver.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: user 层 3 passed + P5 resolver 8 passed；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/context/user_config.py claw_engine/engine/context/workspace.py tests/test_user_config_layer.py
git commit -m "feat: add UserConfigProvider and user-layer env in WorkspaceResolver"
```

---

### Task 3: RouteDecision + 穿 user_id（升级 P6a/P5 调用面，向后兼容）

**Files:**
- Modify: `claw_engine/engine/channels/contracts.py`（加 `RouteDecision`，`WorkspaceRouter.route` 返回它）
- Modify: `claw_engine/engine/channels/routing.py`（`StaticWorkspaceRouter` 返回 RouteDecision）
- Modify: `claw_engine/engine/channels/runner.py`（用 decision + 穿 user_id）
- Modify: `claw_engine/engine/orchestration/workspace_gateway.py`（handle 加 user_id → resolver）
- Modify: `tests/test_channel_runner_e2e.py`（更新 static router 断言）
- Test: `tests/test_user_id_threading.py`

- [ ] **Step 1: 写失败/更新测试**

新增 `tests/test_user_id_threading.py`：
```python
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage, BackendCapabilities, BackendHealth,
)


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


def test_gateway_threads_user_id_into_resolver_user_layer():
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider(workspace_env={"ws1": {"K": "workspace"}})
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"K": "user", "USER_ONLY": "1"}}})
    resolver = WorkspaceResolver(config, InMemorySecretProvider(), workspaces_root="/srv/ws",
                                 user_config=user_config)
    gw = WorkspaceConversationGateway(resolver, convo)
    gw.handle(workspace_id="ws1", channel="c", external_thread_key="t", text="hi",
              backend_name="spy", user_id="u1")
    assert spy.req.env["K"] == "user"          # user 层经 gateway 穿到 resolver 再到 backend
    assert spy.req.env["USER_ONLY"] == "1"
```

更新 `tests/test_channel_runner_e2e.py` 里 `test_static_router_returns_fixed_workspace`：
```python
def test_static_router_returns_fixed_workspace():
    from claw_engine.engine.channels.contracts import IncomingMessage
    r = StaticWorkspaceRouter("wsX")
    msg = IncomingMessage(channel="webhook", raw_user_ref="u", external_thread_key="t", text="hi")
    decision = r.route(msg)
    assert decision.workspace_id == "wsX"
    assert decision.user_id is None
```

- [ ] **Step 2: 跑确认 FAIL**（route 仍返回 str / gateway 无 user_id）

Run: `.venv/bin/pytest tests/test_user_id_threading.py tests/test_channel_runner_e2e.py -q`
Expected: FAIL

- [ ] **Step 3: 写实现（4 处改动）**

a) `channels/contracts.py`：在 `IncomingMessage` 之后加 `RouteDecision`，并改 `WorkspaceRouter.route` 返回类型：
```python
@dataclass(frozen=True)
class RouteDecision:
    workspace_id: str
    user_id: Optional[str] = None
```
```python
@runtime_checkable
class WorkspaceRouter(Protocol):
    def route(self, message: IncomingMessage) -> "RouteDecision":
        """IncomingMessage -> 路由决策(workspace_id + 可选 user_id)。"""
        ...
```

b) `channels/routing.py`：
```python
from claw_engine.engine.channels.contracts import IncomingMessage, RouteDecision

class StaticWorkspaceRouter:
    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id

    def route(self, message: IncomingMessage) -> RouteDecision:
        return RouteDecision(workspace_id=self._workspace_id, user_id=None)
```

c) `channels/runner.py`：`handle_raw` 用 decision：
```python
        decision = self._router.route(msg)
        result = self._conversation.handle(
            workspace_id=decision.workspace_id, channel=msg.channel,
            external_thread_key=msg.external_thread_key, text=msg.text,
            backend_name=self._default_backend, message_id=msg.message_id,
            user_id=decision.user_id,
        )
```

d) `orchestration/workspace_gateway.py`：`handle` 加 `user_id`：
```python
    def handle(self, *, workspace_id: str, channel: str, external_thread_key: str,
               text: str, backend_name: str, message_id: Optional[str] = None,
               model: Optional[str] = None, user_id: Optional[str] = None) -> AgentRunResult:
        ws = self._resolver.resolve(workspace_id, user_id)
        return self._conversation.handle(... 不变 ...)
```

- [ ] **Step 4: PASS + 全量（P6a e2e/security 不回归）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_user_id_threading.py tests/test_channel_runner_e2e.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 新/改测试 PASS；全量 PASS（P6a security e2e、P5 gateway 等仍绿）；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/channels/contracts.py claw_engine/engine/channels/routing.py claw_engine/engine/channels/runner.py claw_engine/engine/orchestration/workspace_gateway.py tests/test_user_id_threading.py tests/test_channel_runner_e2e.py
git commit -m "feat: RouteDecision carries user_id; thread user_id router->gateway->resolver"
```

---

### Task 4: IdentityWorkspaceRouter + RBAC e2e + 最终回归

**Files:**
- Create: `claw_engine/engine/channels/identity_routing.py`
- Test: `tests/test_identity_router_e2e.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_identity_router_e2e.py
import hashlib
import hmac
import json
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.user_config import InMemoryUserConfigProvider
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.identity.contracts import (
    User, UnknownUser, WorkspaceAccessDenied, WorkspaceRouteError,
)
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.channels.identity_routing import IdentityWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.adapters.channels.webhook import WebhookChannel
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage, BackendCapabilities, BackendHealth,
)

SECRET = "s3cr3t"

def _sig(body):
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


class _EnvEchoBackend:
    """捕获 req（含 env）并 echo，用于证明 user config 穿到了 backend env。"""
    name = "fake"

    def __init__(self):
        self.req = None

    def capabilities(self):
        return BackendCapabilities(True, False, False, False, ("none",))

    def healthcheck(self):
        return BackendHealth(ok=True)

    def run(self, req):
        self.req = req
        text = f"echo: {req.prompt}"
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt",
                         result=AgentRunResult(backend_thread_id="bt", final_text=text, usage=TokenUsage()))


def _runner(channel, *, authorized_ws, user_default="ws1"):
    spy = _EnvEchoBackend()
    reg = EngineRegistry()
    reg.register_backend("fake", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider()
    user_config = InMemoryUserConfigProvider({"u1": {"ws1": {"USER_TAG": "alice"}}})
    resolver = WorkspaceResolver(config, InMemorySecretProvider(), workspaces_root="/srv/ws",
                                 user_config=user_config)
    gw = WorkspaceConversationGateway(resolver, convo)
    identity = InMemoryIdentityProvider(
        users={"ref-u1": User(user_id="u1", display_name="Alice", default_workspace=user_default)},
        authorized={"u1": authorized_ws},
    )
    router = IdentityWorkspaceRouter(identity)
    return ChannelRunner(channel, router, gw, default_backend_name="fake"), spy

def _body(user_ref):
    return json.dumps({"user": user_ref, "thread": "t1", "text": "hello"})

def test_authorized_user_routes_replies_and_user_config_reaches_backend():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws1",))
    body = _body("ref-u1")
    result = runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert result.final_text == "echo: hello"
    assert channel.sent_texts[-1][1] == "echo: hello"
    assert spy.req.env["USER_TAG"] == "alice"           # user 层 config 真正穿到 backend env（闭环）

def test_unauthorized_workspace_denied_no_backend_no_reply():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws2",))   # 默认 ws1 不在授权内
    body = _body("ref-u1")
    with pytest.raises(WorkspaceAccessDenied):
        runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert spy.req is None and channel.sent_texts == []      # 越权 -> backend 不跑、无回复

def test_unknown_user_raises_no_backend_no_reply():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws1",))
    body = _body("ghost")
    with pytest.raises(UnknownUser):
        runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert spy.req is None and channel.sent_texts == []

def test_no_default_workspace_raises_route_error():
    channel = WebhookChannel(SECRET)
    runner, spy = _runner(channel, authorized_ws=("ws1",), user_default=None)
    body = _body("ref-u1")
    with pytest.raises(WorkspaceRouteError):
        runner.handle_raw(body, {"X-Signature": _sig(body)})
    assert spy.req is None and channel.sent_texts == []      # 无法路由 != 越权；同样不进引擎
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_identity_router_e2e.py -q`
Expected: FAIL（ModuleNotFoundError: ...identity_routing）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/channels/identity_routing.py
from __future__ import annotations
from claw_engine.engine.channels.contracts import IncomingMessage, RouteDecision
from claw_engine.engine.identity.contracts import (
    IdentityProvider, WorkspaceAccessDenied, WorkspaceRouteError,
)


class IdentityWorkspaceRouter:
    """解析 raw_user_ref -> User -> 校验 default_workspace 授权 -> 路由。替换 P6a 占位路由。"""

    def __init__(self, identity: IdentityProvider) -> None:
        self._identity = identity

    def route(self, message: IncomingMessage) -> RouteDecision:
        user = self._identity.resolve_user(message.raw_user_ref)   # 未知 -> UnknownUser
        ws = user.default_workspace
        if ws is None:
            raise WorkspaceRouteError(f"user {user.user_id} 无 default_workspace，无法路由")
        if not self._identity.can_access_workspace(user, ws):      # 有 default 但越权
            raise WorkspaceAccessDenied(user.user_id, ws)
        return RouteDecision(workspace_id=ws, user_id=user.user_id)
```

- [ ] **Step 4: PASS（3 passed）+ 最终全量 + ruff + purity（P6b 验收）**

Run: `.venv/bin/pytest tests/test_identity_router_e2e.py -q && .venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 4 passed；全量 PASS；All checks passed；purity 2 passed（engine/identity、identity_routing 无业务/CLI 字面量）

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/channels/identity_routing.py tests/test_identity_router_e2e.py
git commit -m "feat: add IdentityWorkspaceRouter (resolve user, RBAC, WorkspaceAccessDenied)"
```

---

## Self-Review

**1. 决策覆盖：**
- 决策1（User+授权查询 IdentityProvider）→ Task 1 ✅
- 决策2（授权失败抛 WorkspaceAccessDenied）→ Task 4 `test_unauthorized_workspace_denied_no_reply` ✅
- 决策3（user config 进 WorkspaceResolver，precedence global→workspace→user→secret）→ Task 2 `test_user_layer_*` ✅
- 决策4（router 先解析 User 再路由）→ Task 4 `IdentityWorkspaceRouter` + e2e ✅
- 模型协调（User 补 default_workspace）→ Task 1 User 字段 ✅
- user_id 全链路穿透（router→gateway→resolver→backend env）→ Task 3 `test_gateway_threads_user_id_into_resolver_user_layer` ✅
- 向后兼容（P5/P6a 既有测试不回归）→ Task 2/3 加法式签名 + Task 3 更新 static router 断言 ✅
- 安全：未知用户/越权不进引擎不回复 → Task 4 `test_unknown_user_raises_no_backend_no_reply` + `test_unauthorized_workspace_denied_no_backend_no_reply` ✅
- 加固1：`authorized` 精确类型 `Mapping[str, tuple[str,...]]` / `authorized_workspaces()->tuple[str,...]` ✅
- 加固2：`UnknownUser` 带 `raw_user_ref` 上下文 → Task 1 断言 ✅
- 加固3：`UserConfigProvider.get_env(user_id, workspace_id)` 带 workspace 维度，避免跨 workspace 污染 → Task 2 ✅
- 加固4：无 default_workspace → `WorkspaceRouteError`（区别于越权）→ Task 4 `test_no_default_workspace_raises_route_error` ✅
- 加固5：RBAC 成功路径用 spy backend 断言 `USER_TAG` 穿到 `req.env`（与 Task 3 穿透测试闭环）→ Task 4 `test_authorized_..._user_config_reaches_backend` ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码（测试片段避免 `a; b` 复合语句以过 ruff E702）。✅

**3. 类型/签名一致性：** `User{user_id,display_name,roles,default_workspace}`；`IdentityProvider`(resolve_user/authorized_workspaces/can_access_workspace)；`UnknownUser`/`WorkspaceAccessDenied(user_id,workspace_id)`；`InMemoryIdentityProvider(users,authorized)`；`UserConfigProvider.get_env(user_id)`；`WorkspaceResolver.__init__(...,user_config=None)` + `resolve(workspace_id,user_id=None)`；`RouteDecision{workspace_id,user_id}`；`WorkspaceRouter.route(msg)->RouteDecision`；`StaticWorkspaceRouter.route->RouteDecision`；`ChannelRunner.handle_raw` 用 decision；`WorkspaceConversationGateway.handle(...,user_id=None)`；`IdentityWorkspaceRouter(identity).route`。各 task 间一致。✅

**已知取舍（实现注意）：**
- env precedence：secret 仍最高（user 在 workspace 与 secret 之间），与 P5 一致。
- `default_workspace` 由 IdentityProvider 给定（用户当前 workspace）；workspace 切换命令属 channel 命令，后续。
- 授权失败/未知用户均在路由阶段抛异常、不进引擎不回复（安全）；上层/channel 决定如何提示用户。
- 真实 SSO/LDAP 身份后端在 adapters 实现 IdentityProvider；P6b 仅内存实现。
- `can_use_skill` 与 skill 可见性强制留 P6d。
