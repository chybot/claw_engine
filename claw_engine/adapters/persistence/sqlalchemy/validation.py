"""Pure-string validators for SQLAlchemySessionStore.

Split out of store.py so the constants + validation functions are
independently testable and store.py stays close to the 300-line soft cap.

NO DB IO occurs here — these functions are called from __init__ (HC-A).
"""
from __future__ import annotations

import re
from typing import Optional

import sqlalchemy.engine.url as sa_url

# ── Allowed DSN URL schemes ──────────────────────────────────────────────────

# Note: "postgres" is intentionally accepted as an alias for "postgresql".
# SQLAlchemy itself emits a deprecation warning but still parses it; we accept
# it so callers migrating from older DSNs aren't broken at the adapter layer.
_ALLOWED_SCHEMES: frozenset[str] = frozenset(
    {
        "sqlite",
        "mysql",
        "mysql+pymysql",
        "postgresql",
        "postgresql+psycopg",
        "postgres",  # alias for postgresql
    }
)

# ── Locked DSN query allowlist (sub-plan §2) ─────────────────────────────────

_ALLOWED_DSN_QUERY_KEYS: frozenset[str] = frozenset(
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

# ── table_prefix validation (interpolated into DDL — strict regex) ────────────

_TABLE_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE_PREFIX_MAX = 32

# ── schema name validation ────────────────────────────────────────────────────

# Note: pattern requires at least 2 characters (one letter/underscore + one or
# more name chars).  Single-letter schemas like "s" are intentionally rejected
# to avoid accidental matches against single-letter SQL aliases.
_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]+$")
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "select", "from", "where", "insert", "update", "delete", "drop",
        "alter", "create", "table", "index", "into", "values", "set",
        "join", "on", "order", "group", "by", "having", "union", "all",
        "distinct", "as", "null", "not", "and", "or", "in", "is", "like",
        "between", "case", "when", "then", "else", "end",
    }
)

# ── Adapter-boundary field length caps (matches §5 schema column widths) ─────

_FIELD_CAPS: dict[str, int] = {
    "workspace_id": 128,
    "channel": 64,
    "external_thread_key": 256,
    "backend_name": 64,
    "session_id": 64,
    "backend_thread_id": 256,
    "message_id": 256,
}


def _validate_field(
    name: str,
    value: Optional[str],
    *,
    nullable: bool = False,
) -> None:
    """Validate a string field at the adapter boundary.

    Non-empty check + length cap from `_FIELD_CAPS`.  No regex —
    SQLAlchemy's parameterized queries prevent SQL injection, and a regex
    on `workspace_id`/`channel` would wrongly reject legitimate thread keys
    like "slack:T01ABC#general/thread/123".

    Raises:
        ValueError: empty / None (when not nullable) / over length cap.
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


def _validate_construction_args(
    url: str,
    schema: str | None,
    table_prefix: str,
) -> None:
    """Validate constructor inputs for SQLAlchemySessionStore.

    Runs pure-string checks — never opens a DB connection (HC-A).
    All-or-nothing: raises ValueError on the first violation; never mutates
    or stores partial state.

    Raises:
        ValueError: Any violation — bad scheme, embedded credentials,
            non-allowlist query keys, bad table_prefix, bad schema.

    HC-C invariant: rejection messages never echo the embedded password
    from a credential-bearing DSN.
    """
    # url presence
    if not url:
        raise ValueError("url must not be empty")

    # Parse DSN (pure-string, no connection attempted)
    try:
        parsed = sa_url.make_url(url)
    except Exception as exc:
        # Suppress chained context (HC-C: avoid leaking url back via __context__)
        raise ValueError(
            f"url is not a valid SQLAlchemy DSN: {exc.__class__.__name__}"
        ) from None

    # Scheme check
    scheme = (parsed.drivername or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"url scheme {scheme!r} is not supported. "
            f"Allowed: sqlite, mysql, mysql+pymysql, postgresql, postgresql+psycopg."
        )

    # Credential check (HC-C) — reject username or password in DSN.
    # Error message MUST NOT echo the password.
    if parsed.username or parsed.password:
        raise ValueError(
            "url must not contain embedded credentials (username / password). "
            "Use driver-level env vars or a SecretProvider instead."
        )

    # Query key allowlist
    if parsed.query:
        for key in parsed.query:
            if key not in _ALLOWED_DSN_QUERY_KEYS:
                raise ValueError(
                    f"url query key {key!r} is not in the allowed list. "
                    f"Allowed keys: {sorted(_ALLOWED_DSN_QUERY_KEYS)}."
                )

    # table_prefix validation (interpolated into DDL — strict regex required)
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

    # schema validation (when supplied)
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
