# claw_engine/engine/workflows/memory_store.py
from __future__ import annotations
from typing import Dict
from claw_engine.engine.workflows.contracts import WorkflowRun


class WorkflowRunNotFound(KeyError):
    pass


class MemoryWorkflowStore:
    def __init__(self) -> None:
        self._runs: Dict[str, WorkflowRun] = {}

    def save(self, run: WorkflowRun) -> None:
        self._runs[run.run_id] = run

    def get(self, run_id: str) -> WorkflowRun:
        try:
            return self._runs[run_id]
        except KeyError:
            raise WorkflowRunNotFound(run_id) from None
