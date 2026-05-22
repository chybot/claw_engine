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
            session_id=row["session_id"],
            workspace_id=row["workspace_id"],
            channel=row["channel"],
            external_thread_key=row["external_thread_key"],
            backend_name=row["backend_name"],
            backend_thread_id=row["backend_thread_id"],
            round_count=row["round_count"],
            max_rounds=row["max_rounds"],
            last_active=row["last_active"],
        )

    def get_or_create(
        self,
        *,
        workspace_id: str,
        channel: str,
        external_thread_key: str,
        backend_name: str,
        max_rounds: int,
    ) -> Session:
        row = self._conn.execute(
            "SELECT * FROM session WHERE workspace_id=? AND channel=? AND external_thread_key=?",
            (workspace_id, channel, external_thread_key),
        ).fetchone()
        if row is not None:
            return self._row_to_session(row)
        session = Session(
            session_id=uuid.uuid4().hex,
            workspace_id=workspace_id,
            channel=channel,
            external_thread_key=external_thread_key,
            backend_name=backend_name,
            max_rounds=max_rounds,
        )
        self._conn.execute(
            "INSERT INTO session (session_id, workspace_id, channel, external_thread_key, "
            "backend_name, backend_thread_id, round_count, max_rounds, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.workspace_id,
                session.channel,
                session.external_thread_key,
                session.backend_name,
                session.backend_thread_id,
                session.round_count,
                session.max_rounds,
                session.last_active,
            ),
        )
        self._conn.commit()
        return session

    def save(self, session: Session) -> None:
        self._conn.execute(
            "UPDATE session SET backend_thread_id=?, round_count=?, max_rounds=?, last_active=? "
            "WHERE session_id=?",
            (
                session.backend_thread_id,
                session.round_count,
                session.max_rounds,
                session.last_active,
                session.session_id,
            ),
        )
        self._conn.commit()

    def is_processed(self, session_id: str, message_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM processed_message WHERE session_id=? AND message_id=?",
                (session_id, message_id),
            ).fetchone()
            is not None
        )

    def mark_processed(self, session_id: str, message_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_message (session_id, message_id) VALUES (?, ?)",
            (session_id, message_id),
        )
        self._conn.commit()
