import pytest
from claw_engine.engine.bootstrap import EngineRegistry, WorkflowNotRegistered
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService
from claw_engine.engine.workflows.contracts import WorkflowStatus


def _service(handlers):
    reg = EngineRegistry()
    for name, fn in handlers.items():
        reg.register_workflow(name, fn)
    return WorkflowService(reg, MemoryWorkflowStore())   # 默认 InlineExecutor


def test_run_success_captures_result_and_progress():
    def ok(params, ctx):
        ctx.report_progress("start", step="s1", fraction=0.0)
        ctx.report_progress("done", step="s2", fraction=1.0)
        return {"echo": params["x"]}
    svc = _service({"ok": ok})
    run = svc.run("ok", {"x": 42})
    assert run.status is WorkflowStatus.SUCCESS
    assert run.result == {"echo": 42}
    assert [p.message for p in run.progress] == ["start", "done"]
    assert run.progress[1].fraction == 1.0

def test_run_failure_records_error_and_keeps_progress():
    def boom(params, ctx):
        ctx.report_progress("before crash")
        raise ValueError("bad input")
    svc = _service({"boom": boom})
    run = svc.run("boom", {})
    assert run.status is WorkflowStatus.FAILED
    assert "ValueError" in run.error and "bad input" in run.error
    assert [p.message for p in run.progress] == ["before crash"]   # 失败前进度保留

def test_run_unregistered_raises():
    svc = _service({})
    with pytest.raises(WorkflowNotRegistered):
        svc.run("missing", {})

def test_get_returns_run():
    svc = _service({"ok": lambda params, ctx: "done"})
    run = svc.run("ok", {})
    assert svc.get(run.run_id).run_id == run.run_id
    assert svc.get(run.run_id).result == "done"

def test_invalid_fraction_fails_run():
    def bad(params, ctx):
        ctx.report_progress("oops", fraction=1.5)   # 越界 → ValueError → 归一 FAILED
        return "unreachable"
    svc = _service({"bad": bad})
    run = svc.run("bad", {})
    assert run.status is WorkflowStatus.FAILED
    assert "fraction" in run.error
