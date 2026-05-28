# claw_engine P7 — SeaTalk Chain Portability Proof 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 用 V1 的 5 seam 落一个真实的 `SeaTalkChannel` 适配器，跑通 algo-bot 同款的入站链——`SeaTalk payload → ChannelGateway → Identity/RBAC → WorkspaceResolver → SkillProvisioner(真 dag_tracer) → spy backend → reply`，并加一条 RBAC 拒绝测试证明 gating 真在工作。**验证 V1 真的是通用引擎，不是只在 hermetic demo 里自洽。**

**Architecture:**
- 新 adapter：`adapters/channels/seatalk.py`（SeaTalk 签名是 `sha256(body+secret)`，与 P6a `WebhookChannel` 的 HMAC 不同）。
- 复用 V1 全部既有组件：ChannelRunner / IdentityWorkspaceRouter / ConversationService / WorkspaceConversationGateway / WorkspaceResolver / SkillProvisioner / Engine.run_turn / MemoryTracer。
- spy backend 捕获 `req.{prompt, cwd, env}`，断言每层都把信息按预期落到该落的位置——真 secret 走 backend env、脱敏走 trace。
- 真 skill 包：从 `/Users/lucas.xu/Desktop/work/git/algo-bot-skills/algo/dag_tracer/` 拷到 tmp，跑 `SkillProvisioner.provision`，断言文件确实落到 `cwd/.agents/skills/dag_tracer/`。

**Tech Stack:** Python 3.11+，标准库（`hashlib`/`shutil`），pytest，ruff。需要本机有 `/Users/lucas.xu/Desktop/work/git/algo-bot-skills/`（否则 skill provisioning 测试 `pytest.skip`）。

**前置参考：** algo-bot `seatalk_callback.py:26`（签名算法）+ `cli/algo_bot.py:420` 范围（payload 解析）；V1 P6a `WebhookChannel` 作镜像参考。

---

## 设计基线（实现前必读，非任务）

### 四个已拍板决策
1. **backend = spy**：断言 prompt/cwd/env 真传到 backend；不起真 codex（hermetic + 可重复）。
2. **SeaTalkChannel 入 `adapters/channels/`**：与 `WebhookChannel` 同级；证明 MessagingGateway seam 真能加新 channel。
3. **3 个测试**：happy path（全链路 + 真值断言）+ RBAC 拒绝（未授权 → backend 不跑、不回复）+ 真 dag_tracer 落盘。
4. **真 skill 包**：从 algo-bot-skills 拷 `dag_tracer/`；测试有 skipif guard。

### SeaTalk 协议细节
- **签名**：`sha256(body + secret)`（algo-bot `seatalk_callback.py:26`）。**不是** HMAC——这正好证明 MessagingGateway seam 不假设签名算法。Header 名 `Signature`（大小写不敏感，按 P6a webhook 教训）。
- **payload 形态**（最小子集）：
  ```json
  {
    "event_type": "message_from_bot_subscriber",
    "event": {
      "message": {"text": {"plain_text": "hello"}, "tag": "text"},
      "sender": {"email": "alice@shopee.com"},
      "thread_id": "thread-abc",
      "message_id": "msg-123"
    }
  }
  ```
- **IncomingMessage 映射**：
  - `channel = "seatalk"`
  - `raw_user_ref = event.sender.email`（IdentityProvider 用这个查 User）
  - `external_thread_key = event.thread_id`
  - `text = event.message.text.plain_text`
  - `message_id = event.message_id`
  - `is_command = text.startswith("/")`
  - `attachments = ()`（V1 不下载附件——P6a 已写明 attachments 由具体 channel 解析，SeaTalk 附件流水线属 future work）

### V1 不变式在 SeaTalk 链上的对应断言
| V1 invariant | SeaTalk 链表现 |
|---|---|
| `verify_inbound` 先于 engine | 坏签名 SeaTalk payload → `InboundAuthError`，spy 不收到 req |
| Identity 路由前置校验 | 未授权 `sender.email` → `UnknownUser` 或 `WorkspaceAccessDenied`，spy 不收到 req，channel.sent_texts 为空 |
| 真 secret 走 backend，脱敏走 trace | `spy.req.env["JIRA_TOKEN"] == "<real>"`；`tracer.traces[0].metadata["redacted_env"]["JIRA_TOKEN"] == "***"` |
| dims 全维度 | `tracer.traces[0].dims = TraceDims(workspace_id, user_id, session_id, backend_name)` 全有值 |
| Skill 真落盘 | 真 dag_tracer 的 `SKILL.md` + `scripts/diagnose_search_trace.py` 出现在 `cwd/.agents/skills/dag_tracer/` |

### 边界（P7 不做）
- 不起真 SeaTalk webhook server（hermetic）。
- 不起真 codex（spy backend）。
- 不复刻 algo-bot 的 prompt 模板包装（V1 直接把 text 作 prompt；PromptProvider seam 留 future）。
- 不做附件/媒体下载/上传（SeaTalk 文件流水线 future）。
- 不做 slash 命令本地分发（algo-bot 在 channel 层处理 `/info` `/help`，V1 channel 不含此逻辑——本计划只证主链路；命令分发可后续作 `MessagingGateway` 装饰器加上）。

---

### Task 1: `SeaTalkChannel` 适配器

**Files:**
- Create: `claw_engine/adapters/channels/seatalk.py`
- Test: `tests/test_seatalk_channel.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_seatalk_channel.py
import hashlib
import json
import pytest
from claw_engine.adapters.channels.seatalk import SeaTalkChannel
from claw_engine.engine.channels.contracts import InboundAuthError, ProgressState, ReplyTarget

SECRET = "seatalk-shared-secret"

def _sig(body: str) -> str:
    return hashlib.sha256((body + SECRET).encode()).hexdigest()

def _payload(text="hello", email="alice@shopee.com", thread="thread-abc", msg_id="msg-1"):
    return json.dumps({
        "event_type": "message_from_bot_subscriber",
        "event": {
            "message": {"text": {"plain_text": text}, "tag": "text"},
            "sender": {"email": email},
            "thread_id": thread,
            "message_id": msg_id,
        },
    })

def test_verify_inbound_accepts_valid_sha256_signature():
    ch = SeaTalkChannel(SECRET)
    body = _payload()
    ch.verify_inbound(body, {"Signature": _sig(body)})
    ch.verify_inbound(body, {"signature": _sig(body)})       # 大小写不敏感

def test_verify_inbound_rejects_missing_and_wrong_signature():
    ch = SeaTalkChannel(SECRET)
    body = _payload()
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {})
    with pytest.raises(InboundAuthError):
        ch.verify_inbound(body, {"Signature": "deadbeef"})

def test_parse_inbound_maps_seatalk_event_to_incoming_message():
    ch = SeaTalkChannel(SECRET)
    msg = ch.parse_inbound(_payload(text="hello world"))
    assert msg.channel == "seatalk"
    assert msg.raw_user_ref == "alice@shopee.com"
    assert msg.external_thread_key == "thread-abc"
    assert msg.text == "hello world"
    assert msg.message_id == "msg-1"
    assert msg.is_command is False
    assert msg.attachments == ()

def test_parse_inbound_detects_slash_command():
    ch = SeaTalkChannel(SECRET)
    assert ch.parse_inbound(_payload(text="/info")).is_command is True

def test_parse_inbound_malformed_raises_value_error():
    ch = SeaTalkChannel(SECRET)
    with pytest.raises(ValueError):
        ch.parse_inbound(json.dumps({"event_type": "x"}))    # 缺 event/message/text
    with pytest.raises(ValueError):
        ch.parse_inbound("not json {{{")

def test_send_text_and_progress_captured():
    ch = SeaTalkChannel(SECRET)
    target = ReplyTarget(channel="seatalk", external_thread_key="t", raw_user_ref="a@b")
    ch.send_text(target, "reply text")
    h = ch.start_progress(target, ("step1",))
    ch.update_progress(h, ProgressState(message="ok"))
    assert ch.sent_texts == [(target, "reply text")]
    assert len(ch.progress_started) == 1 and len(ch.progress_updates) == 1
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_seatalk_channel.py -q`
Expected: FAIL（ModuleNotFoundError: ...adapters.channels.seatalk）

- [ ] **Step 3: 写实现**

```python
# claw_engine/adapters/channels/seatalk.py
from __future__ import annotations
import hashlib
import hmac
import json
from typing import List, Mapping, Tuple
from claw_engine.engine.channels.contracts import (
    IncomingMessage, InboundAuthError, ProgressHandle, ProgressState, ReplyTarget,
)


class SeaTalkChannel:
    """SeaTalk 适配器：签名是 sha256(body + secret)（algo-bot 现状），不是 HMAC。
    payload/reply 都 hermetic（不起真 SeaTalk API）。"""

    name = "seatalk"

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.sent_texts: List[Tuple[ReplyTarget, str]] = []
        self.sent_attachments: List[Tuple[ReplyTarget, Tuple[str, ...]]] = []
        self.progress_started: List[Tuple[ReplyTarget, Tuple[str, ...], str]] = []
        self.progress_updates: List[Tuple[str, ProgressState]] = []

    def verify_inbound(self, raw: str, headers: Mapping[str, str]) -> None:
        lower = {k.lower(): v for k, v in headers.items()}
        sig = lower.get("signature")
        if not sig:
            raise InboundAuthError("missing Signature")
        expected = hashlib.sha256((raw + self._secret).encode()).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise InboundAuthError("signature mismatch")

    def parse_inbound(self, raw: str) -> IncomingMessage:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed SeaTalk body: {exc}") from exc
        try:
            event = payload["event"]
            text = event["message"]["text"]["plain_text"]
            email = event["sender"]["email"]
            thread = event["thread_id"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"SeaTalk payload 缺少必需字段: {exc}") from exc
        return IncomingMessage(
            channel=self.name,
            raw_user_ref=str(email),
            external_thread_key=str(thread),
            text=str(text),
            message_id=event.get("message_id"),
            attachments=(),                          # V1 不下载附件
            is_command=str(text).startswith("/"),
        )

    def send_text(self, target: ReplyTarget, text: str) -> None:
        self.sent_texts.append((target, text))

    def send_attachments(self, target: ReplyTarget, files: Tuple[str, ...]) -> None:
        self.sent_attachments.append((target, tuple(files)))

    def start_progress(self, target: ReplyTarget, steps: Tuple[str, ...]) -> ProgressHandle:
        handle = f"st-prog-{target.external_thread_key}"
        self.progress_started.append((target, tuple(steps), handle))
        return handle

    def update_progress(self, handle: ProgressHandle, state: ProgressState) -> None:
        self.progress_updates.append((handle, state))
```

- [ ] **Step 4: PASS（6 passed）+ 全量 + purity + ruff**

Run: `.venv/bin/pytest tests/test_seatalk_channel.py -q && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 6 passed；全量 PASS；purity 2 passed（"seatalk" 字面量在 adapters，不污染 engine）；All checks passed

- [ ] **Step 5: Commit**

```bash
git add claw_engine/adapters/channels/seatalk.py tests/test_seatalk_channel.py
git commit -m "feat: add SeaTalkChannel adapter (sha256 signature, hermetic capture)"
```

---

### Task 2: SeaTalk 全链路 e2e + RBAC 拒绝 + 真 dag_tracer provisioning

**Files:**
- Test: `tests/test_seatalk_chain_e2e.py`

- [ ] **Step 1: 写失败测试（3 个用例，机器路径有 skipif guard）**

```python
# tests/test_seatalk_chain_e2e.py
import hashlib
import json
import shutil
from pathlib import Path
import pytest

from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine
from claw_engine.engine.orchestration.conversation import ConversationService
from claw_engine.engine.orchestration.workspace_gateway import WorkspaceConversationGateway
from claw_engine.engine.persistence.memory_store import MemorySessionStore
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
from claw_engine.engine.identity.contracts import (
    User, UnknownUser, WorkspaceAccessDenied,
)
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.engine.channels.identity_routing import IdentityWorkspaceRouter
from claw_engine.engine.channels.runner import ChannelRunner
from claw_engine.engine.skills.source import LocalDirSkillSource
from claw_engine.engine.skills.provisioner import SkillProvisioner
from claw_engine.adapters.channels.seatalk import SeaTalkChannel
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunResult, TokenUsage,
    BackendCapabilities, BackendHealth,
)

SECRET = "seatalk-shared-secret"
ALGO_BOT_SKILLS = Path("/Users/lucas.xu/Desktop/work/git/algo-bot-skills/algo")
REAL_SKILL_NAME = "dag_tracer"


def _sig(body):
    return hashlib.sha256((body + SECRET).encode()).hexdigest()

def _payload(text="hello", email="alice@shopee.com", thread="thread-A", msg_id="m1"):
    return json.dumps({
        "event_type": "message_from_bot_subscriber",
        "event": {
            "message": {"text": {"plain_text": text}, "tag": "text"},
            "sender": {"email": email},
            "thread_id": thread,
            "message_id": msg_id,
        },
    })


class _SpyBackend:
    """捕获 req 用于断言（prompt/cwd/env 真传到 backend）。"""
    name = "spy"
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
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED, backend_thread_id="bt-st",
            result=AgentRunResult(backend_thread_id="bt-st", final_text=text, usage=TokenUsage()),
        )


def _build_runner(tmp_path, *, authorized=("ws1",)):
    """构造完整 SeaTalk 链；spy backend + MemoryTracer 供断言。"""
    spy = _SpyBackend()
    reg = EngineRegistry()
    reg.register_backend("spy", lambda: spy)

    tracer = MemoryTracer()
    engine = Engine(reg, tracer=tracer)
    convo = ConversationService(engine, MemorySessionStore())

    workspaces_root = tmp_path / "workspaces"
    cwd = workspaces_root / "ws1"
    cwd.mkdir(parents=True)

    resolver = WorkspaceResolver(
        LayeredConfigProvider(
            global_env={"REGION": "global"},
            workspace_env={"ws1": {"REGION": "id"}},
        ),
        InMemorySecretProvider({"ws1": {"JIRA_TOKEN": "PLAIN-SECRET"}}),
        workspaces_root=str(workspaces_root),
        specs={"ws1": WorkspaceSpec(
            allowed_skills=("dag_tracer",), backend_name="spy", max_rounds=20,
        )},
    )
    gateway = WorkspaceConversationGateway(resolver, convo)

    identity = InMemoryIdentityProvider(
        users={"alice@shopee.com": User(
            user_id="u1", display_name="Alice", default_workspace="ws1",
        )},
        authorized={"u1": tuple(authorized)},
    )
    router = IdentityWorkspaceRouter(identity)

    channel = SeaTalkChannel(SECRET)
    runner = ChannelRunner(channel, router, gateway, default_backend_name="spy")
    return runner, channel, spy, tracer, identity, resolver, cwd


def test_happy_path_full_seatalk_chain(tmp_path):
    """SeaTalk payload -> Channel -> Identity -> Workspace -> spy backend -> reply。
    断言每层都把信息按预期传到该传的位置：真 secret 走 backend env，脱敏走 trace。"""
    runner, channel, spy, tracer, _, _, cwd = _build_runner(tmp_path)
    body = _payload(text="hello world")

    result = runner.handle_raw(body, {"Signature": _sig(body)})

    # 链路终点：reply 已捕获到 channel
    assert result.final_text == "echo: hello world"
    assert len(channel.sent_texts) == 1
    target, text = channel.sent_texts[0]
    assert target.channel == "seatalk"
    assert target.external_thread_key == "thread-A"
    assert target.raw_user_ref == "alice@shopee.com"
    assert text == "echo: hello world"

    # backend 真的收到了通过整条链解析+解析过的数据
    assert spy.req is not None
    assert spy.req.prompt == "hello world"                          # SeaTalk plain_text -> prompt
    assert spy.req.cwd == str(cwd)                                  # ResolvedWorkspace.cwd
    assert spy.req.env["REGION"] == "id"                            # workspace 覆盖 global
    assert spy.req.env["JIRA_TOKEN"] == "PLAIN-SECRET"              # **真 secret 走 backend**

    # 同时：trace 看到的 env 是脱敏的（V1 P6c 不变式）
    rec = tracer.traces[0]
    assert rec.dims.workspace_id == "ws1"
    assert rec.dims.user_id == "u1"
    assert rec.dims.session_id is not None
    assert rec.dims.backend_name == "spy"
    assert rec.metadata["redacted_env"]["JIRA_TOKEN"] == "***"
    assert "PLAIN-SECRET" not in repr(rec.metadata)
    assert "PLAIN-SECRET" not in repr(rec.dims)


def test_rbac_unauthorized_user_no_backend_no_reply(tmp_path):
    """用户被移出 workspace 授权 -> Identity 路由抛 WorkspaceAccessDenied；
    backend 从未运行，channel 无回复。"""
    runner, channel, spy, _, _, _, _ = _build_runner(tmp_path, authorized=())
    body = _payload(text="hello")
    with pytest.raises(WorkspaceAccessDenied):
        runner.handle_raw(body, {"Signature": _sig(body)})
    assert spy.req is None                                          # backend 完全没跑
    assert channel.sent_texts == []                                 # 无回复发出


@pytest.mark.skipif(not (ALGO_BOT_SKILLS / REAL_SKILL_NAME).exists(),
                    reason=f"algo-bot-skills not available at {ALGO_BOT_SKILLS}")
def test_provision_real_dag_tracer_skill_into_workspace(tmp_path):
    """从 algo-bot-skills 拷贝真 dag_tracer skill，跑 SkillProvisioner，
    断言真实文件落到 cwd/.agents/skills/dag_tracer/。证明 V1 能处理真世界 SKILL.md。"""
    runner, _, _, _, identity, resolver, cwd = _build_runner(tmp_path)

    # 拷真 dag_tracer 到 tmp source（hermetic：不读写本机 algo-bot-skills 之外）
    src_root = tmp_path / "skills_src"
    src_root.mkdir()
    shutil.copytree(ALGO_BOT_SKILLS / REAL_SKILL_NAME, src_root / REAL_SKILL_NAME)

    provisioner = SkillProvisioner(LocalDirSkillSource(str(src_root)), identity)
    user = identity.resolve_user("alice@shopee.com")
    ws = resolver.resolve("ws1", user_id="u1")
    result = provisioner.provision(ws, user)

    assert result.provisioned == ("dag_tracer",)
    assert result.denied == () and result.missing == () and result.invalid == ()

    # 真文件落盘
    dst = cwd / ".agents" / "skills" / "dag_tracer"
    assert (dst / "SKILL.md").is_file()
    assert (dst / "scripts" / "diagnose_search_trace.py").is_file()  # algo-bot-skills 已知文件
    # SKILL.md 内容是真的（不是空文件）
    content = (dst / "SKILL.md").read_text(encoding="utf-8")
    assert "dag_tracer" in content or "diagnose" in content.lower() or len(content) > 100
```

- [ ] **Step 2: 跑确认 FAIL**

Run: `.venv/bin/pytest tests/test_seatalk_chain_e2e.py -q`
Expected: FAIL（ImportError: SeaTalkChannel 还没建 / 或本 task 之前未实现），或在 T1 已落地后此处 FAIL 仅因测试断言更严。如果 T1 已绿，T2 测试一开始就应该跑过（因为复用 V1 既有组件 + T1 的 channel）。**特别确认**：如果是「全部 3 个用例都不动也直接 PASS」，说明 V1 真的支撑了——这就是我们要的 proof，按 GREEN 处理；不需要再写新实现代码。

- [ ] **Step 3: 写实现（如必要）**

如果 Step 2 已 3 passed（或 2 passed + 1 skipped），跳过本步——V1 现状已经能跑通整条链，**这就是 portability proof**。

如果 Step 2 FAIL，按失败定位修：可能是 SeaTalk payload 字段映射小偏差、IdentityProvider 配置错、断言文本不准。**不要为了凑测试改 V1 引擎语义**——任何 engine 代码修改都视作回归。

- [ ] **Step 4: PASS + 最终全量 + purity + ruff（P7 验收）**

Run: `.venv/bin/pytest tests/test_seatalk_chain_e2e.py -v && .venv/bin/pytest -q && .venv/bin/pytest tests/purity -q && .venv/bin/ruff check claw_engine tests`
Expected: 3 passed（或 2 passed + dag_tracer skipped 视本机路径而定）；全量 PASS；purity 2 passed（engine 仍 0 业务/CLI 字面量；"seatalk" 只在 adapters/tests）；All checks passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_seatalk_chain_e2e.py
git commit -m "test: e2e SeaTalk chain portability proof (happy + RBAC + real dag_tracer provisioning)"
```

---

## Self-Review

**1. proof 范围覆盖：**
- happy path 全链路（SeaTalk payload → reply）→ T2 `test_happy_path_full_seatalk_chain` 跑完 7 个断言 ✅
- 真 secret 走 backend env，脱敏走 trace（V1 P6c 不变式在 SeaTalk 链上仍成立）→ 同测试断言 `req.env["JIRA_TOKEN"]==PLAIN-SECRET` + `metadata["redacted_env"]==***` ✅
- 全维度 dims（workspace_id/user_id/session_id/backend_name 全有值）→ 同测试 ✅
- RBAC 拒绝时 backend 不跑、不回复 → T2 `test_rbac_unauthorized_user_no_backend_no_reply` ✅
- 真 algo-bot-skills SKILL.md 能被 V1 SkillProvisioner 处理 → T2 `test_provision_real_dag_tracer_skill_into_workspace` ✅
- SeaTalk seam 真的可插（adapters/，不动 engine）→ T1 adapter + purity gate ✅

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码（测试片段避免 `a; b` 复合语句）。✅

**3. 类型/签名一致性：** `SeaTalkChannel(secret)` 实现 `MessagingGateway` 全部 6 个方法；与 P6a `WebhookChannel` 同形（capture-style）；`_sig` helper 用 `sha256(body+secret)`；Header 大小写不敏感（P6a 教训）。`IncomingMessage` 字段映射严格按 P6a 定义。3 个 e2e 测试都构造完整 ChannelRunner + IdentityWorkspaceRouter + WorkspaceConversationGateway 链路（沿用 P6 风格）。✅

**已知取舍（实现注意）：**
- SeaTalk 签名是 `sha256(body+secret)` 而非 HMAC——这正好证明 seam 中立。`hmac.compare_digest` 仍用于常量时间比较。
- 不复刻 algo-bot prompt 模板（V1 直接把 plain_text 作 prompt）；PromptProvider seam 留 future。
- 不做附件下载（V1 attachments=()）；SeaTalk 文件流水线属 future channel work。
- 不做 slash 命令本地分发（algo-bot `/info /help` 在 channel 层处理）——V1 channel 不含此逻辑；若需要可后续给 `MessagingGateway` 加 `parse_inbound` 后置 command 路由装饰器，不需动 engine。
- skill provisioning 测试有机器路径 skipif；机器上没 algo-bot-skills 时跳过、不算回归。
- 本计划**故意预期 T2 一上来就大体 PASS**——这是 portability proof 的核心：V1 现状已能跑通 algo-bot SeaTalk 链路。如需新代码才能过，写在哪、为什么写、是否触及 engine（应当不触及），都要明确报告。
