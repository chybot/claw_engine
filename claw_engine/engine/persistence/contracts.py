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
