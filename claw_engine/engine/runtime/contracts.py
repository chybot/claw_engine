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
