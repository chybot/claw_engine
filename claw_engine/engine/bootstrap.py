# claw_engine/engine/bootstrap.py
from __future__ import annotations
from typing import Callable, Dict
from claw_engine.engine.runtime.contracts import CodeAgentBackend


class BackendNotRegistered(KeyError):
    pass


class EngineRegistry:
    """组合根：adapters 在启动时注册实现，engine 只按名解析抽象类型。"""

    def __init__(self) -> None:
        self._backends: Dict[str, Callable[[], CodeAgentBackend]] = {}

    def register_backend(self, name: str, factory: Callable[[], CodeAgentBackend]) -> None:
        self._backends[name] = factory

    def resolve_backend(self, name: str) -> CodeAgentBackend:
        try:
            factory = self._backends[name]
        except KeyError:
            raise BackendNotRegistered(name) from None
        return factory()
