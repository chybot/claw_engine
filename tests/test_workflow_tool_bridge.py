# tests/test_workflow_tool_bridge.py
import itertools
import pytest
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.context.config import LayeredConfigProvider
from claw_engine.engine.context.secrets import InMemorySecretProvider
from claw_engine.engine.context.workspace import WorkspaceResolver, WorkspaceSpec
from claw_engine.engine.identity.contracts import Principal, User
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.engine.workflows.bridge import (
    ToolInvocationResult, WorkflowNotAllowed, WorkflowToolBridge,
)
from claw_engine.engine.workflows.contracts import WorkflowStatus
from claw_engine.engine.workflows.memory_store import MemoryWorkflowStore
from claw_engine.engine.workflows.service import WorkflowService


def _build_with_handles(*, denied_workflows=None, allowed_workflows=("wf2",),
                        spec_ws="ws1", authorized=("ws1",)):
    """返回所有 handles（供需要在 invoke 与 get 之间改 identity/resolver 状态的撤权测试用）。"""
    counter = itertools.count(1)
    calls = {"n": 0}

    def handler(params, ctx):
        calls["n"] += 1
        return {"call": next(counter), "params": dict(params)}

    reg = EngineRegistry()
    reg.register_workflow("wf2", handler)
    reg.register_workflow("wf3", handler)
    service = WorkflowService(reg, MemoryWorkflowStore())   # InlineExecutor 默认，invoke 后立刻终态

    resolver = WorkspaceResolver(
        LayeredConfigProvider(), InMemorySecretProvider(),
        workspaces_root="/srv/ws",
        specs={spec_ws: WorkspaceSpec(allowed_workflows=allowed_workflows)},
    )
    identity = InMemoryIdentityProvider(
        users={"ref-u": User(user_id="u", display_name="A", default_workspace=spec_ws)},
        authorized={"u": tuple(authorized)},
        denied_workflows={"u": tuple(denied_workflows or ())},
    )
    bridge = WorkflowToolBridge(service, identity, resolver)
    user = identity.resolve_user("ref-u")
    return bridge, user, calls, service, resolver, identity


def _build(**kwargs):
    bridge, user, calls, *_ = _build_with_handles(**kwargs)
    return bridge, user, calls

def test_invoke_runs_allowed_workflow_and_returns_run_id_status():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {"x": 1}, Principal(user=user, workspace_id="ws1"))
    assert isinstance(res, ToolInvocationResult)
    assert res.run_id and res.status is WorkflowStatus.SUCCESS   # Inline executor 已跑完
    assert calls["n"] == 1

def test_invoke_workflow_not_in_allowed_raises_and_handler_not_called():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    with pytest.raises(WorkflowNotAllowed) as ei:
        bridge.invoke("wf3", {}, Principal(user=user, workspace_id="ws1"))  # wf3 不在白名单
    assert ei.value.workflow == "wf3" and ei.value.workspace_id == "ws1"
    assert calls["n"] == 0                                       # handler 没跑

def test_invoke_can_run_workflow_denied_raises_and_handler_not_called():
    bridge, user, calls = _build(allowed_workflows=("wf2",), denied_workflows=("wf2",))
    with pytest.raises(WorkflowNotAllowed):
        bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))   # 在白名单但被 deny
    assert calls["n"] == 0

def test_invoke_binds_owner_workspace_id_on_run():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    run = bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))
    assert run.owner_workspace_id == "ws1"                       # owner 绑定

def test_get_returns_own_run():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {"k": "v"}, Principal(user=user, workspace_id="ws1"))
    run = bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))
    assert run.status is WorkflowStatus.SUCCESS
    assert run.result["params"] == {"k": "v"}

def test_get_cross_workspace_raises_not_allowed():
    bridge, user, calls = _build(allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    # 用其它 workspace 的 principal 试图查这个 run
    other_user = User(user_id="u2", display_name="B", default_workspace="ws2")
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=other_user, workspace_id="ws2"))

# --- 新增加固 ---

def test_invoke_workspace_access_denied_handler_not_called():
    # 用户未被授权访问该 workspace（authorized=()）-> 第一闸口拦截，handler 不跑
    bridge, user, calls = _build(authorized=())
    with pytest.raises(WorkflowNotAllowed):
        bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    assert calls["n"] == 0

def test_invoke_unregistered_workflow_propagates_not_remapped():
    """workflow 在 workspace 白名单但 registry 未注册：bridge 不重映射，让 WorkflowNotRegistered 透传。"""
    from claw_engine.engine.bootstrap import WorkflowNotRegistered
    bridge, user, calls = _build(allowed_workflows=("ghost",))   # ghost 未注册 handler
    with pytest.raises(WorkflowNotRegistered):
        bridge.invoke("ghost", {}, Principal(user=user, workspace_id="ws1"))
    assert calls["n"] == 0   # 已注册的 wf2/wf3 handler 也未被错误调用

def test_get_after_workspace_access_revoked():
    """invoke 时用户在 workspace；之后被移出授权 -> get 不再可读旧 run（撤权一致语义）。"""
    bridge, user, calls, service, resolver, identity = _build_with_handles()
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    identity._authorized["u"] = ()                                # 撤权（V1 内存实现直接改）
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))

def test_get_after_workflow_removed_from_allowed():
    """工作流从 workspace 白名单撤掉 -> 旧 run 不再可读。"""
    bridge, user, calls, service, resolver, identity = _build_with_handles(
        allowed_workflows=("wf2",))
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    resolver._specs["ws1"] = WorkspaceSpec(allowed_workflows=())   # 白名单清空
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))

def test_get_after_can_run_workflow_revoked():
    """can_run_workflow 翻 false -> 旧 run 不再可读。"""
    bridge, user, calls, service, resolver, identity = _build_with_handles()
    res = bridge.invoke("wf2", {}, Principal(user=user, workspace_id="ws1"))
    identity._denied_workflows["u"] = {"wf2"}                      # deny 当前工作流
    with pytest.raises(WorkflowNotAllowed):
        bridge.get(res.run_id, Principal(user=user, workspace_id="ws1"))
