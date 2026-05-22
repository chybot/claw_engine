# claw_engine/engine/observability/memory.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
from claw_engine.engine.observability.contracts import TraceDims


@dataclass
class TraceRecord:
    name: str
    dims: TraceDims
    input: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    output: Any = None
    error: Optional[str] = None
    finished: bool = False


class MemorySpan:
    def __init__(self, record: TraceRecord) -> None:
        self._record = record

    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None:
        self._record.tools.append({"name": name, "input": input, "output": output})

    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None:
        if self._record.finished:
            return
        self._record.output = output
        self._record.error = error
        self._record.finished = True

    def __enter__(self) -> "MemorySpan":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if not self._record.finished:
            self.finish(error=None if exc is None else str(exc))
        return False


class MemoryTracer:
    """测试/开发用：把 trace 记录到内存，供断言。"""

    def __init__(self) -> None:
        self.traces: List[TraceRecord] = []

    def start_trace(
        self,
        name: str,
        *,
        dims: TraceDims,
        input: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> MemorySpan:
        record = TraceRecord(name=name, dims=dims, input=input, metadata=dict(metadata or {}))
        self.traces.append(record)
        return MemorySpan(record)
