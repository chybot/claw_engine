# claw_engine/engine/workflows/bridge.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from claw_engine.engine.context.workspace import WorkspaceResolver
from claw_engine.engine.identity.contracts import IdentityProvider, Principal
from claw_engine.engine.workflows.contracts import WorkflowRun, WorkflowStatus
from claw_engine.engine.workflows.service import WorkflowService


class WorkflowNotAllowed(PermissionError):
    """workflow 不在 workspace 白名单 / 被用户 can_run_workflow 拒绝 / 跨 workspace 访问。"""
    def __init__(self, workspace_id: str, workflow: str) -> None:
        super().__init__(f"workflow {workflow!r} 在 workspace {workspace_id!r} 不被允许")
        self.workspace_id = workspace_id
        self.workflow = workflow


@dataclass(frozen=True)
class ToolInvocationResult:
    """bridge.invoke 的即时返回：仅 run_id + 当前 status，不阻塞 agent turn。"""
    run_id: str
    status: WorkflowStatus


class WorkflowToolBridge:
    """把 WorkflowService 包装成 agent 可调用的 tool；做 RBAC 与跨 workspace 隔离。"""

    def __init__(self, workflow_service: WorkflowService, identity: IdentityProvider,
                 resolver: WorkspaceResolver) -> None:
        self._service = workflow_service
        self._identity = identity
        self._resolver = resolver

    def invoke(self, name: str, params: Optional[Mapping[str, Any]],
               principal: Principal) -> ToolInvocationResult:
        # 1. workspace 访问（不假设上层已校验）
        if not self._identity.can_access_workspace(principal.user, principal.workspace_id):
            raise WorkflowNotAllowed(principal.workspace_id, name)
        ws = self._resolver.resolve(principal.workspace_id, principal.user.user_id)
        # 2. workspace 白名单
        if name not in ws.allowed_workflows:
            raise WorkflowNotAllowed(principal.workspace_id, name)
        # 3. per-user 工作流权限（default-allow）
        if not self._identity.can_run_workflow(principal.user, principal.workspace_id, name):
            raise WorkflowNotAllowed(principal.workspace_id, name)
        # 4. 注册解析由 WorkflowService 抛 WorkflowNotRegistered（不在 bridge 层重映射）
        run = self._service.run(name, params, owner_workspace_id=principal.workspace_id)
        return ToolInvocationResult(run_id=run.run_id, status=run.status)

    def get(self, run_id: str, principal: Principal) -> WorkflowRun:
        """读取时重新校验当前权限（撤权语义与 provisioning 一致）。"""
        run = self._service.get(run_id)                          # 不存在抛 WorkflowRunNotFound
        if not self._identity.can_access_workspace(principal.user, principal.workspace_id):
            raise WorkflowNotAllowed(principal.workspace_id, run.name)
        if run.owner_workspace_id != principal.workspace_id:
            raise WorkflowNotAllowed(principal.workspace_id, run.name)   # 跨 ws 隔离
        ws = self._resolver.resolve(principal.workspace_id, principal.user.user_id)
        if run.name not in ws.allowed_workflows:
            raise WorkflowNotAllowed(principal.workspace_id, run.name)   # 工作流已撤
        if not self._identity.can_run_workflow(principal.user, principal.workspace_id, run.name):
            raise WorkflowNotAllowed(principal.workspace_id, run.name)   # can_run_workflow 撤权
        return run
