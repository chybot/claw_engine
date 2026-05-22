from __future__ import annotations
from typing import Any, Mapping, Optional
from claw_engine.engine.runtime.contracts import AgentEvent, AgentEventKind, AgentError, AgentErrorKind


def _err(kind: AgentErrorKind, message: str, *, retriable: bool, raw: Optional[Mapping[str, Any]] = None) -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.ERROR,
                      error=AgentError(kind=kind, message=message, retriable=retriable, raw=raw))


def protocol_error(message: str, raw: Optional[Mapping[str, Any]] = None) -> AgentEvent:
    return _err(AgentErrorKind.PROTOCOL, message, retriable=False, raw=raw)


def timeout_error(message: str) -> AgentEvent:
    return _err(AgentErrorKind.TIMEOUT, message, retriable=True)


def crash_error(message: str) -> AgentEvent:
    return _err(AgentErrorKind.BACKEND_CRASH, message, retriable=False)
