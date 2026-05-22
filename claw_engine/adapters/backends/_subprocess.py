from __future__ import annotations
import subprocess
import threading
from typing import Callable, Iterator, Mapping, Tuple

# spawn(argv, cwd, env, timeout_s) -> (stdout 行迭代器, 取退出码的可调用)
SpawnFn = Callable[[list, str, Mapping[str, str], int], Tuple[Iterator[str], Callable[[], int]]]


def default_spawn(argv, cwd, env, timeout_s):
    """启动子进程，逐行产出 stdout；watchdog 在 timeout_s 后 kill 并令迭代抛 TimeoutExpired。"""
    proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    timed_out = {"flag": False}

    def _on_timeout() -> None:
        timed_out["flag"] = True
        proc.kill()

    timer = threading.Timer(timeout_s, _on_timeout)
    timer.daemon = True
    timer.start()

    def lines() -> Iterator[str]:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield line.rstrip("\n")
        finally:
            timer.cancel()
            proc.wait()
        if timed_out["flag"]:
            raise subprocess.TimeoutExpired(argv, timeout_s)

    return lines(), (lambda: proc.returncode if proc.returncode is not None else 0)
