# claw_engine/engine/workflows/executor.py
from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, List, Optional


class InlineExecutor:
    """同步立即执行——确定性，默认实现。"""

    def submit(self, fn: Callable[[], None]) -> None:
        fn()


class ThreadWorkflowExecutor:
    """后台线程池执行；测试可用 wait_all 等待。"""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: List[Future] = []
        self._shutdown = False

    def submit(self, fn: Callable[[], None]) -> None:
        if self._shutdown:
            raise RuntimeError("executor 已 shutdown，无法再提交任务")
        self._futures.append(self._pool.submit(fn))

    def wait_all(self, timeout: Optional[float] = None) -> None:
        for fut in list(self._futures):
            fut.result(timeout=timeout)   # 传播 handler 调度层异常（业务异常已在 _execute 内吞）

    def shutdown(self) -> None:
        self._shutdown = True
        self._pool.shutdown(wait=True)
