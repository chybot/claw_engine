# CodeAgent 通用引擎 — 设计 Spec

- **日期**: 2026-05-22
- **状态**: Reviewed（已 review，进入 writing-plans）
- **目录**: `claw_engine/`（全新项目；磁盘当前物理目录暂为 `codeagent_claw/`，待重命名）
- **参考实现**: `algo-bot`（`git.garena.com/su.fu/algo-bot.git`）— 经验来源，不直接复用代码

---

## 1. 背景与目标

### 1.1 背景
`algo-bot` 已经验证了「用 code-agent CLI 作底座做 bot」这条路：codex-cli 子进程 + Session（`workspace_id + thread_id`）+ Config Center + workspace 作用域的 skill/env 注入 + Langfuse。但它的进出口（SeaTalk）和人设（algo 代码分析）是**写死耦合**的，无法迁移到别的场景（如工厂 B2B 工作台）。

### 1.2 目标
做一个**业务零认知的通用引擎**，满足：

1. **底层后端可插拔**：同时支持 **codex-cli** 和 **claude code cli**，未来可加其它 CLI/SDK。引擎不感知具体后端。
2. **场景可迁移**：从「SeaTalk + algo 代码分析」迁到「Web 工作台 + 工厂 B2B 运营」，只实现适配器，**引擎核心一行不改**。
3. **既能产出 bot，也能产出工作台**（白板上的「bot 或工作台」）。
4. **权限按 workspace 隔离**：workspace 是数据 + 权限边界。

### 1.3 非目标（YAGNI）
- 不做 UI 外壳（工作台前端由各场景自建，引擎只到后端 + 聊天/任务接口）。
- 不做 langgraph 式的复杂图编排（后端 CLI 自身负责 agent loop）。
- V1 不做多 CLI 同请求并行；一个 session 绑一个后端。

---

## 2. 分层架构

```
L0  接入层 / Channel Gateway        seam① MessagingGateway（可插拔）
L1  会话编排层 / Orchestration       session + 路由 + 权限闸
L2  Agent 运行时 / Runtime  ★心脏★   seam④ CodeAgentBackend + seam② PromptProvider
L3  能力层 / Capability              skill runtime / workflow / mcp / interactive_feedback
L4  上下文与隔离 / Context           seam③ WorkspaceDataSource + Config Center + secrets + worktree
L5  持久化 / Persistence             session/workspace/task 仓储
⟂   横切 / Cross-cutting             observability + seam⑤ IdentityProvider + backend auth + 安全
```

**读法**：L0/L4 是插拔口（换场景换适配器）；**L2 是心脏**（codex/claude 双后端归一）；L1/L3/L5 + 横切是业务无关的通用机制。

### Seam 编号（全局唯一，禁止复用）
| Seam | 接口 | 层 | 作用 |
|------|------|----|------|
| ① | `MessagingGateway` | L0 | 入站归一 + 出站回复/进度 |
| ② | `PromptProvider` | L2 | 按 workspace/场景装配人设 |
| ③ | `WorkspaceDataSource` | L4 | workspace 数据来源（git 同步 / CSV ingest / ...） |
| ④ | `CodeAgentBackend` | L2 | code-agent CLI 后端抽象（codex / claude / ...） |
| ⑤ | `IdentityProvider` | ⟂ | 身份 + workspace RBAC |

---

## 3. L2 Backend Contract（硬约束）

> 这是引擎最关键的边界。**所有「某个 CLI 长什么样」的知识，必须且只能存在于该 CLI 的 Backend 实现内部。** 边界之上（L1/L3）只认归一后的类型，永远不知道底下是 codex 还是 claude。

### 3.1 核心接口

```python
# engine/runtime/contracts.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Mapping, Any, Optional, Protocol, runtime_checkable

# ---- 输入：引擎 → backend（backend 是 (request) → event-stream 的纯函数）----
@dataclass(frozen=True)
class AgentRunRequest:
    prompt: str                       # 已由 PromptProvider 装配好的完整 prompt
    cwd: str                          # workspace 工作目录（隔离边界）
    env: Mapping[str, str]            # 已合并的 env（全局 + workspace），含 secrets
    backend_thread_id: Optional[str] = None  # 后端 resume 句柄；None=新会话/首轮。对引擎不透明（见 3.4）
    model: Optional[str] = None       # 模型名；None=用 backend/配置默认
    timeout_s: int = 1800
    attachments: tuple[str, ...] = () # 本地文件绝对路径
    metadata: Mapping[str, Any] = field(default_factory=dict)  # workspace_id 等，仅透传给可观测

# ---- 输出：归一事件（引擎唯一消费的类型）----
class AgentEventKind(str, Enum):
    THREAD_STARTED      = "thread_started"     # 携带新 thread_id
    MESSAGE_DELTA       = "message_delta"      # 流式文本增量（可选，看 capability）
    MESSAGE_COMPLETED   = "message_completed"  # 一条完整 assistant 文本
    TOOL_CALL_STARTED   = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TURN_COMPLETED      = "turn_completed"     # 成功终态，携带 AgentRunResult（失败一律走 ERROR）
    ERROR               = "error"              # 终态，携带 AgentError

@dataclass(frozen=True)
class ToolEvent:
    name: str                         # 归一后的工具/skill 名
    input: Optional[Any] = None
    output: Optional[Any] = None
    skill: Optional[str] = None       # 若来自某 skill

@dataclass(frozen=True)
class AgentEvent:
    kind: AgentEventKind
    backend_thread_id: Optional[str] = None   # 后端 resume 句柄（THREAD_STARTED/TURN_COMPLETED 时携带）
    text: Optional[str] = None
    tool: Optional[ToolEvent] = None
    usage: Optional["TokenUsage"] = None
    result: Optional["AgentRunResult"] = None # 仅 TURN_COMPLETED 携带
    error: Optional["AgentError"] = None       # 仅 ERROR 携带
    ts: float = 0.0
    raw: Optional[Mapping[str, Any]] = None   # 原始事件，仅供调试/trace；★边界之上禁止解读★

@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

@dataclass(frozen=True)
class AgentRunResult:
    backend_thread_id: Optional[str]  # 本回合后端 resume 句柄；引擎原样持久化
    final_text: str
    usage: TokenUsage
    status: str = "ok"                # 恒为 "ok"：TURN_COMPLETED 只表成功，所有失败走 ERROR

# ---- 能力协商：引擎据此 gate 功能，禁止假设 ----
@dataclass(frozen=True)
class BackendCapabilities:
    supports_resume: bool
    supports_streaming: bool          # 是否产出 MESSAGE_DELTA
    supports_tools: bool
    supports_mcp: bool
    auth_modes: tuple[str, ...]       # e.g. ("api_key", "cli_login")

# ---- backend 协议 ----
@runtime_checkable
class CodeAgentBackend(Protocol):
    name: str                         # "codex" | "claude" | ...
    def capabilities(self) -> BackendCapabilities: ...
    def run(self, request: AgentRunRequest) -> Iterator[AgentEvent]: ...
    def healthcheck(self) -> "BackendHealth": ...
```

### 3.2 错误归一（统一 taxonomy）

```python
class AgentErrorKind(str, Enum):
    AUTH        = "auth"          # 登录态/api_key 失效
    TIMEOUT     = "timeout"
    RATE_LIMIT  = "rate_limit"
    BACKEND_CRASH = "backend_crash"   # 子进程非零退出 / 崩溃
    PROTOCOL    = "protocol"      # 事件流无法解析
    CANCELLED   = "cancelled"

@dataclass(frozen=True)
class AgentError:
    kind: AgentErrorKind
    message: str
    retriable: bool = False
    raw: Optional[Mapping[str, Any]] = None
```

backend **必须**把各自 CLI 的原生失败映射到这个 taxonomy；上层只按 `AgentErrorKind` 决策（重试/要求重新登录/告知用户），永远不读 CLI 原生 stderr 文本。

### 3.3 Backend 必须遵守的硬规则（MUST / MUST NOT）

1. **唯一知情者**：argv/flags/子进程管理/原生 JSON schema/stdout 行协议——**只能**出现在该 backend 文件内。任何这些字符串出现在 backend 之外 = 违规。
2. **纯函数语义**：`run()` 是 `(AgentRunRequest) → Iterator[AgentEvent]`。backend **MUST NOT** 读写 SessionStore、Channel、Workspace 仓储、Config Center。它需要的一切都在 `AgentRunRequest` 里，产出的一切都在事件流里。
3. **终态唯一**：事件流**必须**以恰好一个 `TURN_COMPLETED`（成功，携带 `result`）或 `ERROR`（任何失败，含 timeout/cancelled，携带 `error`）结束，二者不可同时出现——避免终态重复表达。引擎据此判定回合结束，不靠子进程退出码。
4. **backend_thread_id 不透明**（见 3.4）。
5. **能力诚实**：`capabilities()` 必须如实反映；引擎只在 `supports_streaming=True` 时消费 `MESSAGE_DELTA`，只在 `supports_resume=True` 时回传 `backend_thread_id`。
6. **raw 只读不解读**：`AgentEvent.raw` 仅用于 trace/debug；L1/L3 **禁止** `if event.raw[...]` 这类逻辑。
7. **隔离尊重**：必须以 `request.cwd` 为子进程工作目录，以 `request.env` 为环境，**不得**注入自带的全局环境/凭证（凭证由 L4 注入到 env）。
8. **可取消**：长任务需响应取消（通过迭代器关闭 / 取消信号终止子进程）。

### 3.4 Session 入口 vs Backend resume 句柄（两个概念，禁止混用）

引擎里有**两个不同的标识**：

| 概念 | 归属 | 含义 | 建模 |
|------|------|------|------|
| **用户会话入口** | L1 | 用户在某 channel/workspace 下的一条对话线程 | Session 主键 `session_id`；唯一键 `(workspace_id, channel, external_thread_key)` |
| **backend resume 句柄** | L2 | codex thread id / claude session id，CLI 续接 token | `backend_thread_id`（**nullable**，对引擎不透明） |

- 首轮消息时 `backend_thread_id` 为 `None`（还没拿到）：引擎**先建 Session 行**，待 backend 在 `THREAD_STARTED`/`TURN_COMPLETED` 返回后**回填** `backend_thread_id`。这样 SessionStore 在首轮即可建模，不依赖 CLI 句柄。
- 续接时：引擎从 Session 行取出 `backend_thread_id`，放进 `AgentRunRequest.backend_thread_id` 原样回传。
- 引擎 **MUST NOT** 解析、拼接、推断 `backend_thread_id` 的内部结构。
- **后端切换规则**：同一 Session 的 `backend_name` 一旦确定即固定（`backend_thread_id` 不可跨后端复用）。切后端 = 开新 Session。`session` 表带 `backend_name` 列。

### 3.5 两个首发 Backend 的映射（实现备忘，住在 `adapters/backends/`，非接口）

| 维度 | CodexCliBackend | ClaudeCodeBackend |
|------|-----------------|-------------------|
| 启动 | `codex exec --json [resume <id>] --model M <prompt>` 子进程 | `claude -p --output-format stream-json [--resume <id>] --model M` (prompt 走 stdin/arg) 或 Agent SDK |
| 流协议 | stdout 逐行 JSON：`thread.started`/`item.*`/`tool_call` | stdout 逐行 JSON：`system`/`assistant`/`tool_use`/`result` |
| resume 句柄（→ `backend_thread_id`） | thread_id | session_id |
| 归一映射 | `item.completed(agent_message)` → `MESSAGE_COMPLETED`；`tool_call` → `TOOL_CALL_*`；末条 → `TURN_COMPLETED` | `assistant` → `MESSAGE_*`；`tool_use` → `TOOL_CALL_*`；`result` → `TURN_COMPLETED` |
| auth_modes | `("api_key","cli_login")` | `("api_key","cli_login")` |

---

## 4. engine / adapters 边界（硬约束）

### 4.1 依赖方向（单向，CI 强制）

```
adapters/*   ──依赖──►   engine/  (仅其公开 contracts)
engine/*     ──严禁依赖──►  adapters/*        ❌
engine/*     ──严禁依赖──►  任何具体 CLI / IM / 业务库   ❌
```

- **engine 不得 import adapters**。
- **engine 不得出现业务/渠道/CLI 字面量**：禁止 `"seatalk"`、`"jira"`、`"codex"`、`"claude"`、业务 prompt 文案等出现在 `engine/` 源码。backend 实现整体外移到 `adapters/backends/`（见 §7、§4.2），CLI 名仅作为注册键由配置提供，因此 `engine/` 内**无任何例外**。
- adapters 只能依赖 `engine` 暴露的**抽象接口**（5 个 seam + 数据模型），不依赖 engine 内部实现细节。

### 4.2 组合根 / 注册（Composition Root）

引擎不 `new` 任何具体实现；启动时由**组合根**按 config 装配：

```python
# engine/bootstrap.py（引擎提供注册表与解析，不含任何具体实现）
class EngineRegistry:
    def register_backend(self, name: str, factory: Callable[[], CodeAgentBackend]): ...
    def register_channel(self, name: str, factory: Callable[[], MessagingGateway]): ...
    def register_prompt_provider(self, factory: Callable[[], PromptProvider]): ...
    def register_workspace_source(self, name: str, factory: Callable[[], WorkspaceDataSource]): ...
    def register_identity_provider(self, factory: Callable[[], IdentityProvider]): ...

# 具体实现住在 adapters/，由各场景的入口在启动时注册：
# adapters/algo/main.py
registry.register_channel("seatalk", lambda: SeaTalkGateway(...))
registry.register_backend("codex", lambda: CodexCliBackend(...))
engine = Engine.from_config(config, registry)
```

config 决定本次部署用哪个 channel / backend / data source：

```yaml
# config/algo.yml
engine:
  channel: seatalk
  default_backend: codex          # 可被 workspace/用户覆盖
  workspace_source: git_sync
  identity_provider: seatalk_email
```

### 4.3 「纯净度」自动化测试（CI gate）

spec 要求实现两条**自动化断言**，作为 CI 必过项：

1. `test_engine_has_no_adapter_imports`：用 AST/import-linter 扫 `engine/`，断言无 `import adapters.*`。
2. `test_engine_has_no_business_tokens`：扫 `engine/` 源码，断言不含禁用字面量集合（`seatalk/jira/codex/claude/...`）。因 backend 实现已外移到 `adapters/backends/`，`engine/` 内**无豁免**。

> 这两条把「业务零认知」从口号变成可回归的约束。

### 4.4 五个 Seam 接口（住在 engine，实现住在 adapters）

```python
# engine/channels/contracts.py
class MessagingGateway(Protocol):              # seam①
    def verify_inbound(self, raw: Any, headers: Mapping[str, str]) -> None: ...  # 校验失败抛 InboundAuthError
    def parse_inbound(self, raw: Any) -> IncomingMessage: ...
    def send_text(self, target: ReplyTarget, text: str) -> None: ...
    def send_attachments(self, target: ReplyTarget, files: list[str]) -> None: ...
    def start_progress(self, target: ReplyTarget, steps: list[str]) -> ProgressHandle: ...
    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None: ...

# engine/runtime/contracts.py
class PromptProvider(Protocol):                # seam②
    def build(self, ctx: PromptContext) -> str: ...   # ctx 含 workspace_key/scene/message

# engine/context/contracts.py
class WorkspaceDataSource(Protocol):           # seam③
    def sync(self, workspace_key: str) -> SyncResult: ...     # git pull / CSV ingest / ...
    def resolve_cwd(self, workspace_key: str) -> str: ...

# engine/identity/contracts.py
class IdentityProvider(Protocol):              # seam⑤
    def resolve_user(self, raw_identity: Any) -> User: ...
    def authorized_workspaces(self, user: User) -> list[str]: ...
    def can_use_skill(self, user: User, workspace_key: str, skill: str) -> bool: ...
```

（seam④ `CodeAgentBackend` 见第 3 节。）

---

## 5. 数据与会话模型

- **Session 主键** = `(workspace_id, thread_id)`，附加 `backend_name`（见 3.4）、`max_rounds`、`last_active`、去重 `processed_message_ids`。
- **Workspace** = `{ key, cwd, allowed_skills[], allowed_env_keys[], data_source }`，是数据 + 权限边界。
- **Config Center** 两级合并：全局 `env.config` + `{workspace}.env.config` → 注入子进程 env。Secret 走此通道，**禁止**进 skill 源码。
- **SessionStore** 抽象（memory/sqlite/mysql 可换）。**首发用 sqlite 跑通**，mysql 作为 production adapter 紧随（公司约束：无 PG、redis 级别低），不让首版实现被 mysql 运维细节卡住。

---

## 6. 横切关注点

### 6.1 可观测（Langfuse）
- 每个回合一条 trace；tool/skill 调用、回复为 span（参考 algo-bot `langfuse_tracing.py`）。
- **改进点**：`workspace_id` 必须是 trace **一等属性**（不止 metadata），以支持多 workspace/多场景按租户切分。`user_id`、`session_id`、`backend_name` 同为一等属性。
- 配置走 Config Center，`enabled=false` 时 no-op 降级。

### 6.2 Backend Auth
- 优先级链：用户个人 api_key > 配置 api_key > CLI 登录态（`codex login` / `claude` 登录）。
- auth 状态变化通知走 seam① MessagingGateway（不绑死任何 IM）。

### 6.3 安全 must-fix（从 algo-bot 审计继承，纳入本引擎设计）
| 级别 | 问题 | 引擎对策 |
|------|------|---------|
| 🔴 | webhook 不校验 token（algo-bot AICR） | 入站统一经 seam① `MessagingGateway.verify_inbound(raw, headers)` 校验签名/token，失败抛 `InboundAuthError`；编排层未通过校验不放行 |
| 🔴 | 加密 secret 硬编码源码 | 加密密钥走 Config Center / 环境，禁止入仓；CI secret 扫描 |
| 🔴 | skill 内硬编码明文 JWT/cookie | 凭证一律走 L4 env 注入；skill lint 拒绝明文凭证字面量 |
| 🟠 | CLI 沙箱关闭，仅 prompt 软约束 | 隔离策略可配：cwd 限定 + 可选 OS 级沙箱/容器；写操作走 git worktree |
| 🟠 | api_key 明文经 IM 传输 | 设计 onboard 凭证录入的非明文通道（带外/一次性链接） |

---

## 7. 项目骨架

```
claw_engine/                 # Python 包: claw_engine
├── engine/                  # 通用引擎核心（业务零认知，CI 强制纯净，无任何 CLI 实现）
│   ├── bootstrap.py         #   组合根 / EngineRegistry
│   ├── channels/            # L0  contracts + 无关编排（实现在 adapters）
│   ├── orchestration/       # L1  session 编排 + 路由 + 权限闸
│   ├── runtime/             # L2  ★ contracts(AgentEvent/Backend) + registry + prompt契约 + 契约测试 helper
│   ├── capabilities/        # L3  skill runtime / workflow / mcp / interactive_feedback
│   ├── context/             # L4  workspace / config-center / secrets / worktree
│   ├── persistence/         # L5  session/workspace/task 仓储
│   ├── identity/            # ⟂   IdentityProvider contract + RBAC
│   └── observability/       # ⟂   langfuse 等
├── adapters/                # 具体场景实现 + 可插拔后端
│   ├── backends/            #   seam④ 实现：codex/ 、claude/（CLI 知识封装于此，engine 之外）
│   ├── algo/                #   SeaTalk + 代码分析（对齐 algo-bot 现状）
│   └── factory_b2b/         #   Web 工作台 + 工厂运营（迁移目标，V1 仅 smoke + diff=0）
├── config/                  # 各场景引导配置
├── docs/superpowers/specs/  # 本文档
└── tests/
    ├── contract/            #   backend 契约测试（同一套用例跑 codex + claude）
    └── purity/              #   engine 纯净度断言（§4.3）
```

> 备注：backend 实现（codex/claude）住在 `adapters/backends/<name>/`，受 §3.3 硬规则约束，CLI 知识封死在各自目录内；`engine/` 不含任何 backend 实现，纯净度规则因此无任何例外。

---

## 8. 迁移故事（验证通用性）

| | algo（参考/首个适配） | factory_b2b（迁移目标） | engine 改不改 |
|---|---|---|---|
| Channel | SeaTalk Gateway | Web 工作台 Gateway | ❌ 实现 seam① |
| Backend | codex | codex 或 claude（配置切） | ❌ 内置 |
| Prompt | 代码分析人设 | 工厂运营人设 | ❌ 实现 seam② |
| Workspace 源 | git 同步 | CSV/ERP ingest | ❌ 实现 seam③ |
| 身份 | SeaTalk email | 工作台登录 + RBAC | ❌ 实现 seam⑤ |
| 长任务 | 少 | wf2/3/5/6（run_workflow + 队列） | ❌ 通用 L3 |

**通用性的硬指标**：迁移 factory_b2b 时 `engine/` 的 git diff 必须为 0。

---

## 9. 决策（review 已拍板）
1. **Backend 位置**：外移到 `adapters/backends/`；`engine/runtime/` 只放 contract / 事件类型 / registry / 契约测试 helper。纯净度规则无例外。
2. **Workflow V1**：只做轻量版（`run_workflow` + 队列 + 进度 + retry/rerun）；DAG 编排延后。
3. **命名**：项目/Python 包 = `claw_engine`；引擎核心模块 = `claw_engine.engine`（目录内短名保留 `engine/`）。磁盘物理目录从 `codeagent_claw/` 重命名为 `claw_engine/`。
4. **SessionStore**：sqlite 先跑通，mysql 作为 production adapter 紧随；首版实现计划不被 mysql 运维卡住。
5. **factory_b2b**：V1 就落最小骨架，只做 smoke adapter + `engine/` diff=0 验证，不做完整业务。
```
