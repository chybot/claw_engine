from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class TraceDims:
    workspace_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    backend_name: Optional[str] = None


@runtime_checkable
class TraceSpan(Protocol):
    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None: ...
    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None: ...
    def __enter__(self) -> "TraceSpan": ...
    def __exit__(self, exc_type, exc, tb) -> bool: ...


@runtime_checkable
class Tracer(Protocol):
    def start_trace(self, name: str, *, dims: TraceDims, input: Any = None,
                    metadata: Optional[Mapping[str, Any]] = None) -> TraceSpan: ...
