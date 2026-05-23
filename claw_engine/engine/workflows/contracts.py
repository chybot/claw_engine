from __future__ import annotations
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressEvent:
    message: str
    step: Optional[str] = None
    fraction: Optional[float] = None   # 0.0..1.0
    ts: float = 0.0


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    name: str
    params: Mapping[str, Any]
    status: WorkflowStatus = WorkflowStatus.PENDING
    progress: tuple[ProgressEvent, ...] = ()
    result: Optional[Any] = None
    error: Optional[str] = None
    rerun_of: Optional[str] = None
    owner_workspace_id: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @staticmethod
    def _now(now: Optional[float]) -> float:
        return now if now is not None else time.time()

    def with_status(self, status: WorkflowStatus, *, now: Optional[float] = None) -> "WorkflowRun":
        return replace(self, status=status, updated_at=self._now(now))

    def with_progress(self, event: ProgressEvent, *, now: Optional[float] = None) -> "WorkflowRun":
        return replace(self, progress=self.progress + (event,), updated_at=self._now(now))

    def with_result(self, result: Any, *, now: Optional[float] = None) -> "WorkflowRun":
        # 终态字段互斥：成功时清掉旧 error
        return replace(self, status=WorkflowStatus.SUCCESS, result=result, error=None,
                       updated_at=self._now(now))

    def with_error(self, error: str, *, now: Optional[float] = None) -> "WorkflowRun":
        # 终态字段互斥：失败时清掉旧 result
        return replace(self, status=WorkflowStatus.FAILED, error=error, result=None,
                       updated_at=self._now(now))


@runtime_checkable
class WorkflowStore(Protocol):
    def save(self, run: WorkflowRun) -> None: ...
    def get(self, run_id: str) -> WorkflowRun: ...


@runtime_checkable
class WorkflowExecutor(Protocol):
    def submit(self, fn: Callable[[], None]) -> None: ...


@dataclass
class WorkflowContext:
    """传给 handler：用于上报进度。"""
    run_id: str
    store: WorkflowStore

    def report_progress(self, message: str, *, step: Optional[str] = None,
                        fraction: Optional[float] = None) -> None:
        if fraction is not None and not 0.0 <= fraction <= 1.0:
            raise ValueError(f"fraction 必须在 0.0..1.0 之间，得到 {fraction}")
        run = self.store.get(self.run_id)
        self.store.save(run.with_progress(
            ProgressEvent(message=message, step=step, fraction=fraction, ts=time.time())))


# handler(params, ctx) -> result；抛 Exception 表示任务失败
WorkflowHandler = Callable[[Mapping[str, Any], WorkflowContext], Any]
