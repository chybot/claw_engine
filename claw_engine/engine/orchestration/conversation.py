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
