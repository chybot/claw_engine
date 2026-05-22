# claw_engine P5 — Config Center + WorkspaceResolver + Secrets 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 落地 L4 上下文解析：`ConfigProvider`（2 层 env 合并）+ `SecretProvider`（独立 seam，凭证不落 skill 源码）+ `WorkspaceResolver`（workspace_id → cwd/env/allowed_skills），并用一个**外层 Gateway** 把解析结果接到现有 `ConversationService`（后者签名不变）。

**Architecture:** `engine/context/` 放 `ConfigProvider`/`SecretProvider`/`WorkspaceResolver`（业务无关 Protocol + V1 内存实现；真实后端 SCC/Vault/git 在 adapters 实现同一 Protocol）。`WorkspaceConversationGateway`（engine/orchestration）组合 resolver + ConversationService。secret 走 env 注入但带 `sensitive_keys` 用于日志/trace 脱敏。

**Tech Stack:** Python 3.11+，标准库 `os`，pytest，ruff。全 hermetic（内存 config/secret + FakeBackend）。

**前置参考：** spec §3.4/§5/§6.3，P1–P4 计划与已落地代码（ConversationService、Engine、FakeBackend）。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策
1. **P5 只定 skill visibility 数据模型**（`ResolvedWorkspace.allowed_skills: tuple[str,...]`）；SKILL.md 发现/同步到 `.agents/skills` 的 provisioning 延后 P6。
2. **Config 2 层**：global `env.config` ⊕ `{workspace}.env.config`（workspace 覆盖全局）；per-user 覆盖层**预留**，待 P6 IdentityProvider 落地再插入。
3. **secret = 独立 `SecretProvider` seam**（可接 Vault 等，与普通 config 解耦）；secret 注入 env，但 key 记入 `sensitive_keys` 供脱敏。
4. **不改 `ConversationService`**（仍收 cwd/env）；新增**外层 `WorkspaceConversationGateway`** 负责 workspace_id → 解析 → 调 ConversationService。

### 解析顺序（user 点名要定清楚）
- **cwd** = `workspaces_root / workspace_id`（V1 简单路径；真实仓库同步/CSV ingest = WorkspaceDataSource，后续）。
- **env 优先级（低→高）**：`global env.config` → `{workspace}.env.config` → `secrets`（SecretProvider，最后叠加，同名 key 由 secret 覆盖）。
- **sensitive_keys** = 来自 SecretProvider 的 key 集合（脱敏依据）。
- **allowed_skills / backend_name / max_rounds** 来自每 workspace 的静态 `WorkspaceSpec`。
- **user 层**：预留在 workspace 与 secret 之间的插入点，P5 不实现。

### engine / adapters 责任分界（user 点名）
- `engine/context/`：`ConfigProvider`/`SecretProvider`/`WorkspaceResolver` 的 **Protocol + V1 内存实现**（业务/CLI 无关，purity 守住）。
- adapters（后续）：真实 `ConfigProvider`(公司 SCC)、`SecretProvider`(Vault/SCC)、`WorkspaceDataSource`(git/CSV) 实现同一 Protocol，启动时注册/注入。
- 边界规则不变：engine 不含业务/CLI 字面量；secret **绝不**进 skill 源码或日志明文。

### 边界（P5 不做）
- 不做 SKILL.md 装载/同步（P6）；不做真实 SCC/Vault/git 适配（adapters，后续）；不做 user 层 config 覆盖（P6）；不做 RBAC 鉴权（P6 IdentityProvider）；不改 ConversationService 签名。

---

### Task 1: ConfigProvider（2 层 env 合并）

**Files:**
- Create: `claw_engine/engine/context/__init__.py`
- Create: `claw_engine/engine/context/config.py`
- Test: `tests/test_config_provider.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config_provider.py
from claw_engine.engine.context.config import LayeredConfigProvider

def test_global_only_when_no_workspace_override():
    cp = LayeredConfigProvider(global_env={"A": "1", "B": "2"})
    assert dict(cp.get_env("ws1")) == {"A": "1", "B": "2"}

def test_workspace_overrides_global():
    cp = LayeredConfigProvider(
        global_env={"A": "1", "B": "2"},
        workspace_env={"ws1": {"B": "20", "C": "3"}},
    )
    assert dict(cp.get_env("ws1")) == {"A": "1", "B": "20", "C": "3"}   # workspace wins on B
    assert dict(cp.get_env("other")) == {"A": "1", "B": "2"}            # 未知 workspace 只 global

def test_returns_copy_not_internal_state():
    cp = LayeredConfigProvider(global_env={"A": "1"})
    env = cp.get_env("ws1")
    dict(env)["A"] = "mutated"
    assert dict(cp.get_env("ws1")) == {"A": "1"}                        # 内部不被外部改动污染
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_config_provider.py -q`
Expected: FAIL（ModuleNotFoundError: ...context.config）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/context/config.py
from __future__ import annotations
from typing import Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class ConfigProvider(Protocol):
    def get_env(self, workspace_id: str) -> Mapping[str, str]:
        """该 workspace 生效的 env：global + workspace 合并，workspace 覆盖 global。"""
        ...


class LayeredConfigProvider:
    """V1 内存两层 env。真实后端(公司 SCC)在 adapters 实现同一 Protocol。"""

    def __init__(self, global_env: Optional[Mapping[str, str]] = None,
                 workspace_env: Optional[Mapping[str, Mapping[str, str]]] = None) -> None:
        self._global = dict(global_env or {})
        self._workspace = {k: dict(v) for k, v in (workspace_env or {}).items()}

    def get_env(self, workspace_id: str) -> Mapping[str, str]:
        merged = dict(self._global)
        merged.update(self._workspace.get(workspace_id, {}))   # workspace wins
        return merged
```

- [ ] **Step 4: 跑确认 PASS（3 passed）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_config_provider.py -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 3 passed；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/context/__init__.py claw_engine/engine/context/config.py tests/test_config_provider.py
git commit -m "feat: add ConfigProvider with 2-layer env merge (global + workspace)"
```

---

### Task 2: SecretProvider（独立 seam）+ 脱敏

**Files:**
- Create: `claw_engine/engine/context/secrets.py`
- Test: `tests/test_secret_provider.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_secret_provider.py
from claw_engine.engine.context.secrets import InMemorySecretProvider, redact

def test_get_secrets_per_workspace():
    sp = InMemorySecretProvider({"ws1": {"TOKEN": "t-123"}})
    assert dict(sp.get_secrets("ws1")) == {"TOKEN": "t-123"}
    assert dict(sp.get_secrets("other")) == {}

def test_redact_masks_only_sensitive_keys():
    env = {"A": "1", "TOKEN": "t-123", "COOKIE": "c-xyz"}
    out = redact(env, frozenset({"TOKEN", "COOKIE"}))
    assert out == {"A": "1", "TOKEN": "***", "COOKIE": "***"}   # 非敏感不动，敏感打码

def test_redact_no_sensitive_keys_is_passthrough():
    env = {"A": "1"}
    assert redact(env, frozenset()) == {"A": "1"}
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_secret_provider.py -q`
Expected: FAIL（ModuleNotFoundError: ...context.secrets）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/context/secrets.py
from __future__ import annotations
from typing import AbstractSet, Mapping, Optional, Protocol, runtime_checkable

_REDACTED = "***"


@runtime_checkable
class SecretProvider(Protocol):
    def get_secrets(self, workspace_id: str) -> Mapping[str, str]:
        """该 workspace 的 secret(key->value)。key 视为敏感：注入 env，但日志/trace 必须脱敏。"""
        ...


class InMemorySecretProvider:
    """V1 内存 secret。真实后端(Vault/SCC)在 adapters 实现同一 Protocol。"""

    def __init__(self, secrets: Optional[Mapping[str, Mapping[str, str]]] = None) -> None:
        self._secrets = {k: dict(v) for k, v in (secrets or {}).items()}

    def get_secrets(self, workspace_id: str) -> Mapping[str, str]:
        return dict(self._secrets.get(workspace_id, {}))


def redact(env: Mapping[str, str], sensitive_keys: AbstractSet[str]) -> dict:
    """脱敏副本：sensitive_keys 的值替换为 ***（用于日志/trace；不影响实际注入的 env）。"""
    return {k: (_REDACTED if k in sensitive_keys else v) for k, v in env.items()}
```

- [ ] **Step 4: 跑确认 PASS（3 passed）+ purity + ruff**

Run: `.venv/bin/pytest tests/test_secret_provider.py -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 3 passed；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/context/secrets.py tests/test_secret_provider.py
git commit -m "feat: add SecretProvider seam and redact() helper"
```

---

### Task 3: WorkspaceResolver + ResolvedWorkspace

**Files:**
- Create: `claw_engine/engine/context/workspace.py`
- Test: `tests/test_workspace_resolver.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workspace_resolver.py
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec

def _resolver():
    config = LayeredConfigProvider(
        global_env={"REGION": "global", "SHARED": "g"},
        workspace_env={"ws1": {"REGION": "id", "WS_ONLY": "w"}},
    )
    secrets = InMemorySecretProvider({"ws1": {"TOKEN": "t-123", "SHARED": "secret-wins"}})
    specs = {"ws1": WorkspaceSpec(allowed_skills=("abtest", "dag_tracer"),
                                  backend_name="codex", max_rounds=20)}
    return WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", specs=specs)

def test_resolution_order_global_workspace_secret():
    r = _resolver().resolve("ws1")
    # REGION: workspace 覆盖 global；WS_ONLY: 仅 workspace；TOKEN: secret；SHARED: secret 覆盖 global
    assert r.env["REGION"] == "id"
    assert r.env["WS_ONLY"] == "w"
    assert r.env["TOKEN"] == "t-123"
    assert r.env["SHARED"] == "secret-wins"

def test_cwd_is_root_join_workspace():
    r = _resolver().resolve("ws1")
    assert r.cwd == "/srv/ws/ws1"

def test_sensitive_keys_are_secret_keys():
    r = _resolver().resolve("ws1")
    assert r.sensitive_keys == frozenset({"TOKEN", "SHARED"})
    assert r.redacted_env()["TOKEN"] == "***"
    assert r.redacted_env()["SHARED"] == "***"
    assert r.redacted_env()["REGION"] == "id"            # 非敏感不打码

def test_allowed_skills_and_spec_fields():
    r = _resolver().resolve("ws1")
    assert r.allowed_skills == ("abtest", "dag_tracer")
    assert r.backend_name == "codex" and r.max_rounds == 20

def test_unknown_workspace_uses_global_and_empty_spec():
    r = _resolver().resolve("ghost")
    assert r.env == {"REGION": "global", "SHARED": "g"}
    assert r.allowed_skills == () and r.backend_name is None and r.max_rounds is None
    assert r.sensitive_keys == frozenset()

def test_repr_does_not_leak_secrets():
    r = _resolver().resolve("ws1")
    text = repr(r)
    assert "t-123" not in text and "secret-wins" not in text   # secret 明文绝不出现
    assert "'TOKEN': '***'" in text                            # 脱敏展示
    assert "'REGION': 'id'" in text                            # 非敏感明文可见

def test_path_traversal_workspace_id_rejected():
    import pytest
    from claw_engine.engine.context.workspace import InvalidWorkspaceId
    r = _resolver()
    for bad in ("../secret", "..", ".", "a/b", "a\\b", "ws/../etc"):
        with pytest.raises(InvalidWorkspaceId):
            r.resolve(bad)

def test_valid_workspace_id_with_dots_dashes_allowed():
    r = _resolver().resolve("ws.1_a-b")
    assert r.cwd == "/srv/ws/ws.1_a-b"
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workspace_resolver.py -q`
Expected: FAIL（ModuleNotFoundError: ...context.workspace）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/context/workspace.py
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional
from claw_engine.engine.context.config import ConfigProvider
from claw_engine.engine.context.secrets import SecretProvider, redact

_WORKSPACE_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")


class InvalidWorkspaceId(ValueError):
    pass


def _validate_workspace_id(workspace_id: str) -> None:
    """只允许 [A-Za-z0-9_.-]+，且禁止 '.'/'..'，防路径穿越（../x、a/b、a\\b 等一律拒绝）。"""
    if workspace_id in (".", "..") or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
        raise InvalidWorkspaceId(f"非法 workspace_id（可能路径穿越）: {workspace_id!r}")


@dataclass(frozen=True)
class WorkspaceSpec:
    """每 workspace 的静态配置（V1 内存；真实来源由 adapters 提供）。"""
    allowed_skills: tuple[str, ...] = ()
    backend_name: Optional[str] = None
    max_rounds: Optional[int] = None


@dataclass(frozen=True, repr=False)
class ResolvedWorkspace:
    workspace_id: str
    cwd: str
    env: Mapping[str, str]                 # global ⊕ workspace ⊕ secrets，注入子进程
    sensitive_keys: frozenset[str]         # env 中来自 secret 的 key（脱敏依据）
    allowed_skills: tuple[str, ...] = ()   # skill visibility 数据模型（provisioning 见 P6）
    backend_name: Optional[str] = None
    max_rounds: Optional[int] = None

    def redacted_env(self) -> dict:
        return redact(self.env, self.sensitive_keys)

    def __repr__(self) -> str:
        # 自定义 repr：绝不泄露 env 明文 secret，只展示脱敏后的 env（repr=False 关掉默认实现）
        return (f"ResolvedWorkspace(workspace_id={self.workspace_id!r}, cwd={self.cwd!r}, "
                f"env={self.redacted_env()!r}, sensitive_keys={set(self.sensitive_keys)!r}, "
                f"allowed_skills={self.allowed_skills!r}, backend_name={self.backend_name!r}, "
                f"max_rounds={self.max_rounds!r})")


class WorkspaceResolver:
    """workspace_id -> ResolvedWorkspace。解析顺序见计划「设计基线」。"""

    def __init__(self, config: ConfigProvider, secrets: SecretProvider, *,
                 workspaces_root: str,
                 specs: Optional[Mapping[str, WorkspaceSpec]] = None) -> None:
        self._config = config
        self._secrets = secrets
        self._root = workspaces_root
        self._specs = dict(specs or {})

    def resolve(self, workspace_id: str) -> ResolvedWorkspace:
        _validate_workspace_id(workspace_id)                   # 防路径穿越
        base_env = dict(self._config.get_env(workspace_id))    # global + workspace（workspace wins）
        secret_env = dict(self._secrets.get_secrets(workspace_id))
        env = {**base_env, **secret_env}                       # secret 最后叠加（同名覆盖）
        spec = self._specs.get(workspace_id, WorkspaceSpec())
        return ResolvedWorkspace(
            workspace_id=workspace_id,
            cwd=os.path.join(self._root, workspace_id),
            env=env,
            sensitive_keys=frozenset(secret_env.keys()),
            allowed_skills=spec.allowed_skills,
            backend_name=spec.backend_name,
            max_rounds=spec.max_rounds,
        )
```

- [ ] **Step 4: 跑确认 PASS（5 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_workspace_resolver.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 8 passed；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/context/workspace.py tests/test_workspace_resolver.py
git commit -m "feat: add WorkspaceResolver + ResolvedWorkspace (cwd/env/secrets/allowed_skills)"
```

---

### Task 4: WorkspaceConversationGateway（外层组合，不改 ConversationService）

**Files:**
- Create: `claw_engine/engine/orchestration/workspace_gateway.py`
- Test: `tests/test_workspace_gateway.py`

- [ ] **Step 1: 写失败测试（spy backend 验证解析出的 cwd/env 真正到达 backend）**

```python
# tests/test_workspace_gateway.py
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
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
        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text="ok")
        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt_1",
                         result=AgentRunResult(backend_thread_id="bt_1", final_text="ok", usage=TokenUsage()))


def _gateway(spy):
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider(global_env={"REGION": "global"},
                                   workspace_env={"ws1": {"REGION": "id"}})
    secrets = InMemorySecretProvider({"ws1": {"TOKEN": "t-123"}})
    specs = {"ws1": WorkspaceSpec(backend_name="spy", max_rounds=5)}
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", specs=specs)
    return WorkspaceConversationGateway(resolver, convo)


def test_gateway_resolves_cwd_env_into_backend():
    spy = _SpyBackend()
    gw = _gateway(spy)
    res = gw.handle(workspace_id="ws1", channel="c", external_thread_key="t",
                    text="hi", backend_name="fallback")
    assert res.final_text == "ok"
    # 解析出的 cwd/env（含 workspace 覆盖 + secret）真正到达 backend
    assert spy.req.cwd == "/srv/ws/ws1"
    assert spy.req.env["REGION"] == "id"
    assert spy.req.env["TOKEN"] == "t-123"

def test_gateway_uses_workspace_backend_over_fallback():
    spy = _SpyBackend()
    gw = _gateway(spy)
    # spec.backend_name="spy" 应优先于传入的 fallback
    res = gw.handle(workspace_id="ws1", channel="c", external_thread_key="t2",
                    text="hi", backend_name="fallback")
    assert res.final_text == "ok"   # 用了 spy（已注册），fallback 未注册若被用会抛错

def test_gateway_falls_back_to_passed_backend_when_spec_has_none():
    spy = _SpyBackend()
    fallback = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)
    reg.register_backend("fallback", lambda: fallback)
    convo = ConversationService(Engine(reg), MemorySessionStore())
    config = LayeredConfigProvider(global_env={"REGION": "global"})
    secrets = InMemorySecretProvider({})
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws", specs={})  # ws2 无 spec
    gw = WorkspaceConversationGateway(resolver, convo)
    res = gw.handle(workspace_id="ws2", channel="c", external_thread_key="t",
                    text="hi", backend_name="fallback")
    assert res.final_text == "ok"
    assert fallback.req is not None and spy.req is None   # 用了 fallback，没用 spy
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_workspace_gateway.py -q`
Expected: FAIL（ModuleNotFoundError: ...workspace_gateway）

- [ ] **Step 3: 写实现**

```python
# claw_engine/engine/orchestration/workspace_gateway.py
from __future__ import annotations
from typing import Optional
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.orchestration.conversation import ConversationService, DEFAULT_MAX_ROUNDS
from claw_engine.engine.runtime.contracts import AgentRunResult


class WorkspaceConversationGateway:
    """外层：workspace_id -> 解析 cwd/env/backend -> 交给 ConversationService（其签名不变）。"""

    def __init__(self, resolver: WorkspaceResolver, conversation: ConversationService) -> None:
        self._resolver = resolver
        self._conversation = conversation

    def handle(self, *, workspace_id: str, channel: str, external_thread_key: str,
               text: str, backend_name: str, message_id: Optional[str] = None,
               model: Optional[str] = None) -> AgentRunResult:
        ws = self._resolver.resolve(workspace_id)
        return self._conversation.handle(
            workspace_id=workspace_id, channel=channel, external_thread_key=external_thread_key,
            text=text, cwd=ws.cwd, env=ws.env,
            backend_name=ws.backend_name or backend_name,   # workspace 指定的 backend 优先
            max_rounds=ws.max_rounds or DEFAULT_MAX_ROUNDS,
            message_id=message_id, model=model,
        )
```

- [ ] **Step 4: 跑确认 PASS（2 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_workspace_gateway.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 3 passed；全量 PASS；purity 2 passed；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/engine/orchestration/workspace_gateway.py tests/test_workspace_gateway.py
git commit -m "feat: add WorkspaceConversationGateway (resolve workspace -> ConversationService)"
```

---

### Task 5: secret 注入闭环 + 脱敏 e2e + 最终回归

**Files:**
- Test: `tests/test_secret_injection_e2e.py`

- [ ] **Step 1: 写测试（secret 到达 backend env，但脱敏视图打码——boundary #1 验收）**

```python
# tests/test_secret_injection_e2e.py
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec

def test_secret_reaches_env_but_redacted_view_masks_it():
    config = LayeredConfigProvider(global_env={"REGION": "id"})
    secrets = InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "super-secret"}})
    resolver = WorkspaceResolver(config, secrets, workspaces_root="/srv/ws",
                                 specs={"ws1": WorkspaceSpec()})
    rw = resolver.resolve("ws1")

    # 实际注入 env 含真实 secret（供子进程/skill 通过 env 读取——不落 skill 源码）
    assert rw.env["JIRA_TOKEN"] == "super-secret"
    # 脱敏视图（用于日志/trace）打码，且不含明文
    redacted = rw.redacted_env()
    assert redacted["JIRA_TOKEN"] == "***"
    assert "super-secret" not in repr(redacted)
    # 非敏感字段在两边都明文
    assert rw.env["REGION"] == "id" and redacted["REGION"] == "id"
```

- [ ] **Step 2: 跑确认（实现已在 T1–T3，预期直接 PASS——本 task 是 boundary 锁定）**

Run: `.venv/bin/pytest tests/test_secret_injection_e2e.py -q`
Expected: 1 passed。若 FAIL 说明 T2/T3 实现与 spec 不符，回去修实现而非改测试。

- [ ] **Step 3: 最终全量 + ruff + purity（P5 验收）**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check claw_engine tests && .venv/bin/pytest tests/purity -q`
Expected: 全量 PASS；All checks passed；purity 2 passed（engine/context 与 workspace_gateway 全程无业务/CLI 字面量、无明文 secret）

- [ ] **Step 4: Commit**

```bash
git add tests/test_secret_injection_e2e.py
git commit -m "test: lock secret-injection-via-env + redaction boundary"
```

---

## Self-Review

**1. 决策/边界覆盖（对照你点名的 4 条 + 4 决策）：**
- 决策1（只定 skill visibility 数据模型）→ `ResolvedWorkspace.allowed_skills` Task 3；provisioning 明确延后 ✅
- 决策2（Config 2 层，user 层预留）→ `LayeredConfigProvider` Task 1 + 解析顺序文档 ✅
- 决策3（独立 SecretProvider seam）→ Task 2 `SecretProvider` + Task 3 注入 ✅
- 决策4（不改 ConversationService，外层解析）→ Task 4 `WorkspaceConversationGateway`；ConversationService 文件不动 ✅
- 边界① secret 不落 skill 源码 → secret 走 SecretProvider→env 注入 + 脱敏；Task 5 e2e 锁定 ✅
- 边界② workspace→cwd/env 解析顺序 → 设计基线明确 global→workspace→secret + cwd=root/ws；Task 3 `test_resolution_order_*` ✅
- 边界③ skill visibility 数据模型 → `allowed_skills: tuple[str,...]` + WorkspaceSpec ✅
- 边界④ engine/adapters 责任分界 → Protocol 在 engine + V1 内存实现；真实 SCC/Vault/git 在 adapters（设计基线写明）✅
- 安全加固A：`ResolvedWorkspace.__repr__` 只露脱敏 env（默认 repr 会泄露 secret）→ Task 3 `test_repr_does_not_leak_secrets` ✅
- 安全加固B：workspace_id 防路径穿越（`InvalidWorkspaceId`，禁 `../`、`/`、`.`、`..`）→ Task 3 `test_path_traversal_workspace_id_rejected` ✅
- 加固C：gateway spec 无 backend 时回退传入 backend_name → Task 4 `test_gateway_falls_back_to_passed_backend_when_spec_has_none` ✅
- 加固D：`redact`/`sensitive_keys` 用 `AbstractSet[str]`/`frozenset[str]` 精确类型 ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码。✅

**3. 类型/签名一致性：** `ConfigProvider.get_env(workspace_id)`；`SecretProvider.get_secrets(workspace_id)` + `redact(env, sensitive_keys)`；`WorkspaceSpec(allowed_skills,backend_name,max_rounds)`；`ResolvedWorkspace(workspace_id,cwd,env,sensitive_keys,allowed_skills,backend_name,max_rounds)+redacted_env()`；`WorkspaceResolver(config,secrets,workspaces_root,specs).resolve()`；`WorkspaceConversationGateway(resolver,conversation).handle(workspace_id,channel,external_thread_key,text,backend_name,message_id,model)`；调用 `ConversationService.handle` 与 P3 签名一致；`Engine`/`FakeBackend`/`MemorySessionStore` 沿用现有。各 task 间一致。✅

**已知取舍（实现注意）：**
- env 优先级 secret 最后叠加（同名 key secret 覆盖 workspace/global）——若不希望 secret 覆盖普通 env，可调顺序；当前选择「secret 最高优先」以保证凭证以 secret 源为准。
- cwd 仅 `root/workspace_id` 路径，**不**校验目录是否存在/不做仓库同步（WorkspaceDataSource 后续）。
- per-user config 覆盖层未实现（P6 IdentityProvider）。
- ConfigProvider/SecretProvider V1 仅内存；真实 SCC/Vault 适配在 adapters，启动注入。
- `redact` 用于日志/trace；P6 接 langfuse 时，trace 的 env/metadata 必须走 `redacted_env()`，不能落明文 secret（届时在 observability 层强制）。
