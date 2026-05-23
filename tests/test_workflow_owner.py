from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService


def _svc():
    reg = EngineRegistry()
    reg.register_workflow("wf", lambda p, c: "ok")
    return WorkflowService(reg, MemoryWorkflowStore())


def test_run_records_owner_workspace_id():
    svc = _svc()
    run = svc.run("wf", {}, owner_workspace_id="ws1")
    assert run.owner_workspace_id == "ws1"


def test_run_default_owner_is_none():
    svc = _svc()
    run = svc.run("wf", {})
    assert run.owner_workspace_id is None        # 加法式：旧调用不传 owner


def test_rerun_preserves_owner():
    svc = _svc()
    first = svc.run("wf", {}, owner_workspace_id="ws1")
    second = svc.rerun(first.run_id)
    assert second.owner_workspace_id == "ws1"    # 撤权后再跑仍记原 owner
