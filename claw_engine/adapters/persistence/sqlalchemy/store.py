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

import threading
import uuid

import sqlalchemy
import sqlalchemy.engine.url as sa_url

from claw_engine.adapters.persistence.sqlalchemy.errors import SessionStoreError
from claw_engine.adapters.persistence.sqlalchemy.schema import _build_tables
from claw_engine.adapters.persistence.sqlalchemy.validation import (
    _validate_construction_args,
    _validate_field,
)
from claw_engine.engine.persistence.contracts import Session


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
            schema: Postgres schema name to qualify both tables.  None → default
                    search_path.  Ignored by sqlite and mysql at the DDL level.
            table_prefix: Prefix for both table names.  Default "claw_".
                          Empty string allowed; max 32 chars.

        Raises:
            ValueError: Any validation failure (scheme, credentials, query keys,
                        table_prefix, schema).
        """
        # ── Validate ALL inputs before storing any self._x (P8d HC-A lesson) ──
        _validate_construction_args(url, schema, table_prefix)

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

        Raises:
            SessionStoreError(stage='init'): when create_engine or
                metadata.create_all fail.  Message intentionally omits the
                underlying exception text since SQLAlchemy errors can echo
                the bound URL (HC-C); inspect ``__cause__`` for the full
                detail when debugging.
        """
        if self._engine is not None:
            return self._engine

        with self._engine_lock:
            # Double-check inside lock
            if self._engine is not None:
                return self._engine

            engine: sqlalchemy.engine.Engine | None = None
            try:
                engine = sqlalchemy.create_engine(self._url)
                sessions, processed = _build_tables(
                    self._table_prefix, schema=self._schema
                )
                # HC-B: CREATE TABLE IF NOT EXISTS for both tables.
                # metadata.create_all with checkfirst=True is documented to
                # emit only CREATE TABLE IF NOT EXISTS — never DROP / ALTER.
                sessions.metadata.create_all(engine, checkfirst=True)
            except sqlalchemy.exc.SQLAlchemyError as exc:
                # Release any pool resources from the half-built engine.
                if engine is not None:
                    try:
                        engine.dispose()
                    except Exception:
                        pass
                # HC-C: do NOT include str(exc) — SQLAlchemy can embed the
                # bound URL with credentials in its error messages.
                raise SessionStoreError(
                    "failed to initialize engine or create schema: "
                    f"{exc.__class__.__name__}",
                    stage="init",
                ) from exc

            # Store atomically (last assignments before returning)
            self._sessions = sessions
            self._processed = processed
            self._engine = engine

        return self._engine

    def _engine_and_tables(
        self,
    ) -> tuple[sqlalchemy.engine.Engine, sqlalchemy.Table, sqlalchemy.Table]:
        """Return (engine, sessions_table, processed_table), ensuring init."""
        engine = self._ensure_engine()
        assert self._sessions is not None  # set by _ensure_engine
        assert self._processed is not None
        return engine, self._sessions, self._processed

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

        engine, sessions, _ = self._engine_and_tables()

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

        engine, sessions, _ = self._engine_and_tables()

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

        engine, _, processed = self._engine_and_tables()

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

        engine, _, processed = self._engine_and_tables()

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
