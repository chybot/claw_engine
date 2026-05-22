import dataclasses
import pytest
from claw_engine.engine.workflows.contracts import (
    WorkflowRun, WorkflowStatus, ProgressEvent,
)

def _run(**kw):
    base = dict(run_id="r1", name="wf", params={"a": 1})
    base.update(kw)
    return WorkflowRun(**base)

def test_run_is_frozen():
    r = _run()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = WorkflowStatus.RUNNING

def test_defaults():
    r = _run()
    assert r.status is WorkflowStatus.PENDING
    assert r.progress == ()
    assert r.result is None and r.error is None and r.rerun_of is None

def test_with_status_and_progress_are_immutable():
    r = _run()
    r2 = r.with_status(WorkflowStatus.RUNNING, now=1.0)
    assert r.status is WorkflowStatus.PENDING       # 原不变
    assert r2.status is WorkflowStatus.RUNNING and r2.updated_at == 1.0
    ev = ProgressEvent(message="step1", step="s1", fraction=0.5, ts=2.0)
    r3 = r2.with_progress(ev, now=2.0)
    assert r2.progress == ()
    assert r3.progress == (ev,)

def test_with_result_sets_success_and_with_error_sets_failed():
    r = _run().with_result({"ok": True}, now=3.0)
    assert r.status is WorkflowStatus.SUCCESS and r.result == {"ok": True}
    r2 = _run().with_error("BoomError: bad", now=4.0)
    assert r2.status is WorkflowStatus.FAILED and r2.error == "BoomError: bad"

def test_terminal_fields_are_mutually_exclusive():
    recovered = _run().with_error("boom").with_result({"ok": True})
    assert recovered.result == {"ok": True} and recovered.error is None   # 成功清掉 error
    failed = _run().with_result({"ok": True}).with_error("boom")
    assert failed.error == "boom" and failed.result is None               # 失败清掉 result
