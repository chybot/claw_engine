"""Schema builder for SQLAlchemySessionStore.

Builds both tables against a fresh MetaData per adapter instance so that
different table_prefix values don't conflict.  Called once by _ensure_engine().
"""
from __future__ import annotations

import sqlalchemy


def _build_tables(
    prefix: str,
    *,
    schema: str | None = None,
) -> tuple[sqlalchemy.Table, sqlalchemy.Table]:
    """Return (sessions_table, processed_messages_table) bound to a fresh MetaData.

    Args:
        prefix: table name prefix, e.g. "claw_".  Empty string is allowed.
        schema: schema name to qualify both tables (Postgres).  None → no
            qualification (sqlite / mysql ignore schema or use default).

    Returns:
        Tuple of (sessions table, processed_messages table).  The MetaData
        linking them is accessible via either table's ``.metadata`` attribute.
    """
    metadata = sqlalchemy.MetaData(schema=schema)

    # ForeignKey target needs a fully-qualified name when schema is set, so
    # that SQLAlchemy resolves the parent table reference correctly.
    fk_target = (
        f"{schema}.{prefix}sessions.session_id"
        if schema
        else f"{prefix}sessions.session_id"
    )

    sessions = sqlalchemy.Table(
        f"{prefix}sessions",
        metadata,
        sqlalchemy.Column(
            "session_id",
            sqlalchemy.String(64),
            primary_key=True,
            nullable=False,
        ),
        sqlalchemy.Column(
            "workspace_id",
            sqlalchemy.String(128),
            nullable=False,
        ),
        sqlalchemy.Column(
            "channel",
            sqlalchemy.String(64),
            nullable=False,
        ),
        sqlalchemy.Column(
            "external_thread_key",
            sqlalchemy.String(256),
            nullable=False,
        ),
        sqlalchemy.Column(
            "backend_name",
            sqlalchemy.String(64),
            nullable=False,
        ),
        sqlalchemy.Column(
            "backend_thread_id",
            sqlalchemy.String(256),
            nullable=True,
        ),
        sqlalchemy.Column(
            "round_count",
            sqlalchemy.Integer,
            nullable=False,
            server_default="0",
        ),
        sqlalchemy.Column(
            "max_rounds",
            sqlalchemy.Integer,
            nullable=False,
            server_default="50",
        ),
        sqlalchemy.Column(
            "last_active",
            sqlalchemy.Float,
            nullable=False,
            server_default="0.0",
        ),
        sqlalchemy.UniqueConstraint(
            "workspace_id",
            "channel",
            "external_thread_key",
            name=f"uq_{prefix}sessions_natural_key",
        ),
        schema=schema,
    )

    processed_messages = sqlalchemy.Table(
        f"{prefix}processed_messages",
        metadata,
        sqlalchemy.Column(
            "session_id",
            sqlalchemy.String(64),
            sqlalchemy.ForeignKey(
                fk_target,
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sqlalchemy.Column(
            "message_id",
            sqlalchemy.String(256),
            nullable=False,
        ),
        sqlalchemy.PrimaryKeyConstraint("session_id", "message_id"),
        schema=schema,
    )

    return sessions, processed_messages
