from __future__ import annotations
import uuid
from typing import Any, Mapping, Optional
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.contracts import (
    WorkflowContext, WorkflowExecutor, WorkflowHandler, WorkflowRun, WorkflowStatus, WorkflowStore,
)
from claw_engine.engine.workflows.executor import InlineExecutor


class WorkflowService:
    def __init__(self, registry: EngineRegistry, store: WorkflowStore,
                 executor: Optional[WorkflowExecutor] = None) -> None:
        self._registry = registry
        self._store = store
        self._executor = executor or InlineExecutor()

    def run(self, name: str, params: Optional[Mapping[str, Any]] = None, *,
            rerun_of: Optional[str] = None) -> WorkflowRun:
        handler = self._registry.resolve_workflow(name)   # 未注册即抛 WorkflowNotRegistered
        run = WorkflowRun(run_id=uuid.uuid4().hex, name=name, params=dict(params or {}),
                          status=WorkflowStatus.PENDING, rerun_of=rerun_of)
        self._store.save(run)
        self._executor.submit(lambda: self._execute(run.run_id, handler))
        return self._store.get(run.run_id)

    def get(self, run_id: str) -> WorkflowRun:
        return self._store.get(run_id)

    def _execute(self, run_id: str, handler: WorkflowHandler) -> None:
        self._store.save(self._store.get(run_id).with_status(WorkflowStatus.RUNNING))
        ctx = WorkflowContext(run_id=run_id, store=self._store)
        params = self._store.get(run_id).params
        try:
            result = handler(params, ctx)
        except Exception as exc:   # 业务 handler 失败 = 任务 FAILED（归一，不向上冒泡）
            self._store.save(self._store.get(run_id).with_error(f"{type(exc).__name__}: {exc}"))
            return
        self._store.save(self._store.get(run_id).with_result(result))
