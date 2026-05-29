"""SQLAlchemy Core SessionStore adapter.

Lifecycle (3 phases, strictly separated):
1. Construction   — pure-string validation only; no DB IO (HC-A).
2. First-use      — lazy Engine + CREATE TABLE IF NOT EXISTS (idempotent, HC-B).
3. Steady-state   — CRUD via the shared Engine; lock not held after init.

HC-A: Construction is hermetic (no DB IO).
HC-B: Schema migration is create-only, idempotent; no DROP / ALTER.
HC-C: DSN credentials rejected at construction; never leak via repr/str.
HC-D: Session round-trip byte-identical across sqlite / mysql / postgres.
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Optional

import sqlalchemy
import sqlalchemy.engine.url as sa_url

from claw_engine.engine.persistence.contracts import Session
from claw_engine.adapters.persistence.sqlalchemy.errors import SessionStoreError
from claw_engine.adapters.persistence.sqlalchemy.schema import _build_tables

# ── Allowed URL schemes ───────────────────────────────────────────────────────

_ALLOWED_SCHEMES = frozenset(
    {
        "sqlite",
        "mysql",
        "mysql+pymysql",
        "postgresql",
        "postgresql+psycopg",
        # also accept "postgres" alias that SQLAlchemy normalises
        "postgres",
    }
)

# ── Locked DSN query allowlist (sub-plan §2) ─────────────────────────────────

_ALLOWED_DSN_QUERY_KEYS = frozenset(
    {
        # universal
        "connect_timeout",
        "charset",
        # postgres
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "application_name",
        # mysql / pymysql
        "ssl_ca",
        "ssl_cert",
        "ssl_key",
        "ssl_verify_cert",
        "ssl_verify_identity",
    }
)

# ── table_prefix validation ───────────────────────────────────────────────────

_TABLE_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE_PREFIX_MAX = 32

# ── schema name validation ────────────────────────────────────────────────────

_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]+$")
_SQL_KEYWORDS = frozenset(
    {
        "select",
        "from",
        "where",
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "table",
        "index",
        "into",
        "values",
        "set",
        "join",
        "on",
        "order",
        "group",
        "by",
        "having",
        "union",
        "all",
        "distinct",
        "as",
        "null",
        "not",
        "and",
        "or",
        "in",
        "is",
        "like",
        "between",
        "case",
        "when",
        "then",
        "else",
        "end",
    }
)

# ── Adapter-boundary field length caps (sub-plan §7, matches §5 schema) ──────

_FIELD_CAPS: dict[str, int] = {
    "workspace_id": 128,
    "channel": 64,
    "external_thread_key": 256,
    "backend_name": 64,
    "session_id": 64,
    "backend_thread_id": 256,
    "message_id": 256,
}


def _validate_field(name: str, value: Optional[str], *, nullable: bool = False) -> None:
    """Validate a string field: non-empty check and length cap.

    No regex — SQLAlchemy parameterized queries prevent SQL injection;
    regex on workspace_id/channel would wrongly reject legitimate thread keys
    like "slack:T01ABC#general/thread/123".
    """
    if value is None:
        if not nullable:
            raise ValueError(f"{name} must not be None")
        return
    if not value:
        raise ValueError(f"{name} must not be empty")
    cap = _FIELD_CAPS.get(name)
    if cap is not None and len(value) > cap:
        raise ValueError(
            f"{name} exceeds maximum length {cap} (got {len(value)})"
        )


class SQLAlchemySessionStore:
    """SessionStore backed by SQLAlchemy Core (sqlite / mysql / postgresql).

    Construction is hermetic: no DB IO in __init__. The Engine is created
    lazily on the first Protocol method call and reused for all subsequent
    calls (single shared Engine per adapter instance, thread-safe).
    """

    def __init__(
        self,
        url: str,
        *,
        schema: str | None = None,
        table_prefix: str = "claw_",
    ) -> None:
        """Construct the adapter.  No DB IO occurs here (HC-A).

        Args:
            url: SQLAlchemy DSN — sqlite:///…, mysql+pymysql://…, postgresql+psycopg://….
                 Credentials MUST NOT be embedded (HC-C).  Use env-vars consumed
                 by the driver or wrap through SecretProvider.
            schema: Postgres schema name.  None → default search_path.
                    Ignored for sqlite and mysql.
            table_prefix: Prefix for both table names.  Default "claw_".
                          Empty string allowed; max 32 chars.

        Raises:
            ValueError: Any validation failure (scheme, credentials, query keys,
                        table_prefix, schema).
        """
        # ── Validate ALL inputs before storing any self._x (P8d HC-A lesson) ──

        if not url:
            raise ValueError("url must not be empty")

        # Parse DSN (pure-string, no connection attempted)
        try:
            parsed = sa_url.make_url(url)
        except Exception as exc:
            raise ValueError(f"url is not a valid SQLAlchemy DSN: {exc}") from None

        # Scheme check
        scheme = (parsed.drivername or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"url scheme {scheme!r} is not supported. "
                f"Allowed: sqlite, mysql, mysql+pymysql, postgresql, postgresql+psycopg."
            )

        # Credential check (HC-C) — reject username or password in DSN
        if parsed.username or parsed.password:
            raise ValueError(
                "url must not contain embedded credentials (username / password). "
                "Use driver-level env vars or a SecretProvider instead."
            )

        # Query key allowlist check
        if parsed.query:
            for key in parsed.query:
                if key not in _ALLOWED_DSN_QUERY_KEYS:
                    raise ValueError(
                        f"url query key {key!r} is not in the allowed list. "
                        f"Allowed keys: {sorted(_ALLOWED_DSN_QUERY_KEYS)}."
                    )

        # table_prefix validation (interpolated into DDL — strict regex needed)
        if table_prefix != "" and not _TABLE_PREFIX_RE.match(table_prefix):
            raise ValueError(
                f"table_prefix {table_prefix!r} is invalid. "
                "Must match [A-Za-z_][A-Za-z0-9_]* or be empty string."
            )
        if len(table_prefix) > _TABLE_PREFIX_MAX:
            raise ValueError(
                f"table_prefix exceeds maximum length {_TABLE_PREFIX_MAX} "
                f"(got {len(table_prefix)})"
            )

        # schema validation
        if schema is not None:
            if not _SCHEMA_NAME_RE.match(schema):
                raise ValueError(
                    f"schema {schema!r} is invalid. "
                    "Must match [A-Za-z_][A-Za-z0-9_]+ (at least 2 chars)."
                )
            if schema.lower() in _SQL_KEYWORDS:
                raise ValueError(
                    f"schema {schema!r} is a reserved SQL keyword."
                )

        # ── All validated — now store ──────────────────────────────────────────
        self._url = url
        self._schema = schema
        self._table_prefix = table_prefix

        # Lazy engine state (HC-A: NOT created here)
        self._engine: sqlalchemy.engine.Engine | None = None
        self._engine_lock = threading.Lock()
        self._sessions: sqlalchemy.Table | None = None
        self._processed: sqlalchemy.Table | None = None

    # ── Lazy engine creation (HC-A / HC-B) ───────────────────────────────────

    def _ensure_engine(self) -> sqlalchemy.engine.Engine:
        """Return the shared Engine, creating it lazily on first call.

        Double-checked locking prevents two threads racing into double-create.
        Once the engine exists, the lock is NOT held on reads.
        """
        if self._engine is not None:
            return self._engine

        with self._engine_lock:
            # Double-check inside lock
            if self._engine is not None:
                return self._engine

            engine = sqlalchemy.create_engine(self._url)
            sessions, processed = _build_tables(self._table_prefix)

            # HC-B: CREATE TABLE IF NOT EXISTS for both tables.
            # metadata.create_all with checkfirst=True emits CREATE TABLE IF NOT EXISTS.
            # Never DROP, never ALTER.
            metadata = sessions.metadata
            metadata.create_all(engine, checkfirst=True)

            # Store atomically (last line before returning)
            self._sessions = sessions
            self._processed = processed
            self._engine = engine

        return self._engine

    # ── Protocol methods ──────────────────────────────────────────────────────

    def get_or_create(
        self,
        *,
        workspace_id: str,
        channel: str,
        external_thread_key: str,
        backend_name: str,
        max_rounds: int,
    ) -> Session:
        """Return existing session or create a new one.

        Lookup is by natural key (workspace_id, channel, external_thread_key).
        If a session exists, it is returned unchanged (existing fields win).
        """
        _validate_field("workspace_id", workspace_id)
        _validate_field("channel", channel)
        _validate_field("external_thread_key", external_thread_key)
        _validate_field("backend_name", backend_name)

        engine = self._ensure_engine()
        sessions = self._sessions
        assert sessions is not None  # set by _ensure_engine

        def _select_by_natural_key(conn: object) -> object:
            return conn.execute(  # type: ignore[union-attr]
                sqlalchemy.select(sessions).where(
                    sessions.c.workspace_id == workspace_id,
                    sessions.c.channel == channel,
                    sessions.c.external_thread_key == external_thread_key,
                )
            ).one_or_none()

        with engine.begin() as conn:
            row = _select_by_natural_key(conn)
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
            try:
                conn.execute(
                    sqlalchemy.insert(sessions).values(
                        session_id=session.session_id,
                        workspace_id=session.workspace_id,
                        channel=session.channel,
                        external_thread_key=session.external_thread_key,
                        backend_name=session.backend_name,
                        backend_thread_id=session.backend_thread_id,
                        round_count=session.round_count,
                        max_rounds=session.max_rounds,
                        last_active=session.last_active,
                    )
                )
                return session
            except sqlalchemy.exc.IntegrityError:
                # Concurrent insert race: another thread inserted the same
                # natural key between our SELECT and INSERT.  Transaction
                # will be rolled back when this `with` block exits; we
                # re-fetch the winner's row in a fresh transaction below.
                pass

        # Concurrent INSERT lost the race — fetch the winner's row.
        with engine.begin() as conn:
            row = _select_by_natural_key(conn)
            if row is not None:
                return self._row_to_session(row)
            # Should never reach here — a row was inserted by another thread.
            raise SessionStoreError(
                "get_or_create: session not found after concurrent insert",
                stage="query",
            )

    def save(self, session: Session) -> None:
        """Persist updates to an existing session.

        Contract: only updates rows previously created by get_or_create().
        Does NOT insert new rows — callers must use get_or_create() for that.
        Unknown session_id → silent no-op (matches sqlite_store behavior).
        """
        _validate_field("session_id", session.session_id)
        if session.backend_thread_id is not None:
            _validate_field(
                "backend_thread_id", session.backend_thread_id, nullable=True
            )

        engine = self._ensure_engine()
        sessions = self._sessions
        assert sessions is not None

        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.update(sessions)
                .where(sessions.c.session_id == session.session_id)
                .values(
                    backend_thread_id=session.backend_thread_id,
                    round_count=session.round_count,
                    max_rounds=session.max_rounds,
                    last_active=session.last_active,
                )
            )

    def is_processed(self, session_id: str, message_id: str) -> bool:
        """Return True if message_id has been marked processed for session_id."""
        _validate_field("session_id", session_id)
        _validate_field("message_id", message_id)

        engine = self._ensure_engine()
        processed = self._processed
        assert processed is not None

        with engine.connect() as conn:
            row = conn.execute(
                sqlalchemy.select(processed).where(
                    processed.c.session_id == session_id,
                    processed.c.message_id == message_id,
                )
            ).one_or_none()
            return row is not None

    def mark_processed(self, session_id: str, message_id: str) -> None:
        """Mark message_id as processed for session_id (idempotent).

        Calling twice does not error (INSERT OR IGNORE semantics via
        try/except on IntegrityError).
        """
        _validate_field("session_id", session_id)
        _validate_field("message_id", message_id)

        engine = self._ensure_engine()
        processed = self._processed
        assert processed is not None

        with engine.begin() as conn:
            try:
                conn.execute(
                    sqlalchemy.insert(processed).values(
                        session_id=session_id,
                        message_id=message_id,
                    )
                )
            except sqlalchemy.exc.IntegrityError:
                # Already exists — idempotent, swallow the duplicate key error.
                pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_session(row: object) -> Session:
        """Convert a SQLAlchemy Core row (mapping) to a Session dataclass."""
        m = row._mapping  # type: ignore[union-attr]
        return Session(
            session_id=m["session_id"],
            workspace_id=m["workspace_id"],
            channel=m["channel"],
            external_thread_key=m["external_thread_key"],
            backend_name=m["backend_name"],
            backend_thread_id=m["backend_thread_id"],
            round_count=m["round_count"],
            max_rounds=m["max_rounds"],
            last_active=m["last_active"],
        )

    def __repr__(self) -> str:
        """Return a safe representation that never leaks DSN credentials (HC-C)."""
        try:
            url_obj = sa_url.make_url(self._url)
            safe_url = url_obj.render_as_string(hide_password=True)
        except Exception:
            safe_url = "<unparseable-url>"
        return (
            f"SQLAlchemySessionStore("
            f"url={safe_url!r}, "
            f"schema={self._schema!r}, "
            f"table_prefix={self._table_prefix!r})"
        )
