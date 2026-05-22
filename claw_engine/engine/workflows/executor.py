# claw_engine/engine/workflows/executor.py
from __future__ import annotations
from typing import Callable


class InlineExecutor:
    """同步立即执行——确定性，默认实现。"""

    def submit(self, fn: Callable[[], None]) -> None:
        fn()
