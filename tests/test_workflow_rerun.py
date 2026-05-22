import itertools
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService, WorkflowNotTerminal
from claw_engine.engine.workflows.contracts import WorkflowRun, WorkflowStatus


def test_rerun_creates_new_run_preserving_history():
    counter = itertools.count(1)
    def wf(params, ctx):
        return {"call": next(counter)}
    reg = EngineRegistry()
    reg.register_workflow("wf", wf)
    store = MemoryWorkflowStore()
    svc = WorkflowService(reg, store)

    first = svc.run("wf", {"k": "v"})
    assert first.result == {"call": 1}

    second = svc.rerun(first.run_id)
    assert second.run_id != first.run_id          # 新 run_id
    assert second.rerun_of == first.run_id        # 指向旧 run
    assert second.name == "wf" and second.params == {"k": "v"}   # 同 name+params
    assert second.result == {"call": 2}           # 独立的新一次运行

    # 旧 run 记录保留、不被改动
    old = svc.get(first.run_id)
    assert old.run_id == first.run_id
    assert old.result == {"call": 1}
    assert old.rerun_of is None
    assert old.status is WorkflowStatus.SUCCESS

def test_rerun_of_failed_run_can_succeed():
    state = {"fail_first": True}
    def flaky(params, ctx):
        if state["fail_first"]:
            state["fail_first"] = False
            raise RuntimeError("first fails")
        return "ok"
    reg = EngineRegistry()
    reg.register_workflow("flaky", flaky)
    svc = WorkflowService(reg, MemoryWorkflowStore())
    first = svc.run("flaky", {})
    assert first.status is WorkflowStatus.FAILED
    second = svc.rerun(first.run_id)
    assert second.status is WorkflowStatus.SUCCESS and second.result == "ok"
    assert svc.get(first.run_id).status is WorkflowStatus.FAILED   # 旧 run 仍为失败

def test_rerun_non_terminal_run_raises():
    reg = EngineRegistry()
    reg.register_workflow("wf", lambda p, c: "ok")
    store = MemoryWorkflowStore()
    svc = WorkflowService(reg, store)
    # 手工塞一个 RUNNING（非终态）run
    store.save(WorkflowRun(run_id="running1", name="wf", params={}, status=WorkflowStatus.RUNNING))
    with pytest.raises(WorkflowNotTerminal):
        svc.rerun("running1")
