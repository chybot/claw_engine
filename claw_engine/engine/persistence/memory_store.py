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
