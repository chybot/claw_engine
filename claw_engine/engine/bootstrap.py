# claw_engine/engine/bootstrap.py
from __future__ import annotations
from typing import Callable, Dict
from claw_engine.engine.runtime.contracts import CodeAgentBackend
from claw_engine.engine.workflows.contracts import WorkflowHandler


class BackendNotRegistered(KeyError):
    pass


class WorkflowNotRegistered(KeyError):
    pass


class EngineRegistry:
    """组合根：adapters 在启动时注册实现，engine 只按名解析抽象类型。"""

    def __init__(self) -> None:
        self._backends: Dict[str, Callable[[], CodeAgentBackend]] = {}
        self._workflows: Dict[str, WorkflowHandler] = {}

    def register_backend(self, name: str, factory: Callable[[], CodeAgentBackend]) -> None:
        self._backends[name] = factory

    def resolve_backend(self, name: str) -> CodeAgentBackend:
        try:
            factory = self._backends[name]
        except KeyError:
            raise BackendNotRegistered(name) from None
        return factory()

    def register_workflow(self, name: str, handler: WorkflowHandler) -> None:
        self._workflows[name] = handler

    def resolve_workflow(self, name: str) -> WorkflowHandler:
        try:
            return self._workflows[name]
        except KeyError:
            raise WorkflowNotRegistered(name) from None
