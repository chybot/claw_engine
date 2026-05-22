from __future__ import annotations
import json
import shutil
import subprocess
from typing import Callable, Iterator, Mapping, Optional, Tuple
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentError, AgentErrorKind, AgentRunRequest,
    AgentRunResult, BackendCapabilities, BackendHealth, ToolEvent, TokenUsage,
)

# spawn(argv, cwd, env, timeout_s) -> (stdout 行迭代器, 取退出码的可调用)
SpawnFn = Callable[[list, str, Mapping[str, str], int], Tuple[Iterator[str], Callable[[], int]]]


def _protocol_error(message: str, raw: Optional[dict] = None) -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.ERROR,
        error=AgentError(kind=AgentErrorKind.PROTOCOL, message=message, retriable=False, raw=raw),
    )


def _default_spawn(argv, cwd, env, timeout_s):
    proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    def lines() -> Iterator[str]:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait(timeout=timeout_s)
    return lines(), (lambda: proc.returncode if proc.returncode is not None else 0)


class CodexCliBackend:
    name = "codex"

    def __init__(self, command: str = "codex", spawn: Optional[SpawnFn] = None) -> None:
        self._command = command
        self._spawn = spawn or _default_spawn

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_resume=True, supports_streaming=False,
            supports_tools=True, supports_mcp=True, auth_modes=("api_key", "cli_login"),
        )

    def healthcheck(self) -> BackendHealth:
        return BackendHealth(ok=shutil.which(self._command) is not None,
                             detail="" if shutil.which(self._command) else "codex not on PATH")

    def _build_argv(self, req: AgentRunRequest) -> list:
        argv = [self._command, "exec"]
        if req.backend_thread_id:
            argv += ["resume", req.backend_thread_id]
        argv += ["--dangerously-bypass-approvals-and-sandbox", "--json"]
        if req.model:
            argv += ["--model", req.model]
        argv.append(req.prompt)
        return argv

    def run(self, req: AgentRunRequest) -> Iterator[AgentEvent]:
        argv = self._build_argv(req)
        line_iter, returncode = self._spawn(argv, req.cwd, req.env, req.timeout_s)
        thread_id = req.backend_thread_id
        final_text = ""
        for raw_line in line_iter:
            if not raw_line.strip():
                continue
            try:
                evt = json.loads(raw_line)
            except json.JSONDecodeError:
                # 非 JSON / 解析失败 = 协议破损，立即终止
                yield _protocol_error(f"无法解析 codex 输出行: {raw_line[:200]!r}")
                return
            etype = evt.get("type")
            if etype == "thread.started":
                tid = evt.get("thread_id")
                if not tid:
                    yield _protocol_error("thread.started 缺少 thread_id", evt)
                    return
                thread_id = tid
                yield AgentEvent(kind=AgentEventKind.THREAD_STARTED,
                                 backend_thread_id=thread_id, raw=evt)
            elif etype == "item.completed":
                item = evt.get("item")
                if not isinstance(item, dict) or not item.get("type"):
                    yield _protocol_error("item.completed 缺少 item.type", evt)
                    return
                itype = item["type"]
                if itype == "tool_call":
                    name = item.get("name")
                    if not name:
                        yield _protocol_error("tool_call 缺少 name", evt)
                        return
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_CALL_COMPLETED,
                        tool=ToolEvent(name=name, input=item.get("input"),
                                       output=item.get("output")),
                        raw=evt,
                    )
                elif itype == "agent_message":
                    if "text" not in item:
                        yield _protocol_error("agent_message 缺少 text", evt)
                        return
                    final_text = item.get("text") or ""
                    yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED,
                                     text=final_text, raw=evt)
                # 已知 item.completed 但未知 item.type → 前向兼容忽略
            # 未知顶层 type → 前向兼容忽略
        code = returncode()
        if code != 0:
            yield AgentEvent(
                kind=AgentEventKind.ERROR,
                error=AgentError(kind=AgentErrorKind.BACKEND_CRASH,
                                 message=f"codex exited with {code}", retriable=False),
            )
            return
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id=thread_id,
            result=AgentRunResult(backend_thread_id=thread_id, final_text=final_text,
                                  usage=TokenUsage()),
        )
