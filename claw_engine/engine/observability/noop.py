from __future__ import annotations
from typing import Any, Mapping, Optional
from claw_engine.engine.observability.contracts import TraceDims


class NoOpSpan:
    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None:
        return None

    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None:
        return None

    def __enter__(self) -> "NoOpSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class NoOpTracer:
    """Default tracer: records nothing."""

    def start_trace(self, name: str, *, dims: TraceDims, input: Any = None,
                    metadata: Optional[Mapping[str, Any]] = None) -> NoOpSpan:
        return NoOpSpan()
