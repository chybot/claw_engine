# tests/test_workflow_store_executor.py
import pytest
from claw_engine.engine.workflows.contracts import WorkflowRun, WorkflowStatus
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore, WorkflowRunNotFound
from claw_engine.engine.workflows.executor import InlineExecutor

def test_store_save_and_get():
    store = MemoryWorkflowStore()
    run = WorkflowRun(run_id="r1", name="wf", params={})
    store.save(run)
    assert store.get("r1").run_id == "r1"
    store.save(run.with_status(WorkflowStatus.RUNNING))
    assert store.get("r1").status is WorkflowStatus.RUNNING   # upsert

def test_store_get_missing_raises():
    with pytest.raises(WorkflowRunNotFound):
        MemoryWorkflowStore().get("nope")

def test_inline_executor_runs_immediately():
    calls = []
    InlineExecutor().submit(lambda: calls.append(1))
    assert calls == [1]
