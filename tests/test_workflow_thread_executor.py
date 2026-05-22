# tests/test_workflow_thread_executor.py
import threading
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService
from claw_engine.engine.workflows.executor import ThreadWorkflowExecutor
from claw_engine.engine.workflows.contracts import WorkflowStatus


def test_thread_executor_runs_in_background_then_completes():
    started = threading.Event()
    release = threading.Event()

    def handler(params, ctx):
        started.set()
        assert release.wait(timeout=5)
        ctx.report_progress("done")
        return {"ok": True}

    reg = EngineRegistry()
    reg.register_workflow("bg", handler)
    store = MemoryWorkflowStore()
    ex = ThreadWorkflowExecutor()
    try:
        svc = WorkflowService(reg, store, executor=ex)
        run = svc.run("bg", {})
        assert started.wait(timeout=5)                       # handler 已在后台启动
        # 尚未释放 → 非终态（证明 run() 没有阻塞到完成）
        assert svc.get(run.run_id).status in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING)
        release.set()
        ex.wait_all(timeout=5)
        final = svc.get(run.run_id)
        assert final.status is WorkflowStatus.SUCCESS
        assert final.result == {"ok": True}
        assert [p.message for p in final.progress] == ["done"]
    finally:
        ex.shutdown()

def test_submit_after_shutdown_raises():
    import pytest
    ex = ThreadWorkflowExecutor()
    ex.shutdown()
    with pytest.raises(RuntimeError):
        ex.submit(lambda: None)
