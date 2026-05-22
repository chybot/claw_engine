from __future__ import annotations
import json
import shutil
import subprocess
from typing import Iterator, Optional
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest,
    AgentRunResult, BackendCapabilities, BackendHealth, ToolEvent, TokenUsage,
)
from claw_engine.adapters.backends._subprocess import SpawnFn, default_spawn
from claw_engine.adapters.backends._errors import protocol_error, timeout_error, crash_error


class CodexCliBackend:
    name = "codex"

    def __init__(self, command: str = "codex", spawn: Optional[SpawnFn] = None) -> None:
        self._command = command
        self._spawn = spawn or default_spawn

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
        try:
            for raw_line in line_iter:
                if not raw_line.strip():
                    continue
                try:
                    evt = json.loads(raw_line)
                except json.JSONDecodeError:
                    # 非 JSON / 解析失败 = 协议破损，立即终止
                    yield protocol_error(f"无法解析 codex 输出行: {raw_line[:200]!r}")
                    return
                etype = evt.get("type")
                if etype == "thread.started":
                    tid = evt.get("thread_id")
                    if not tid:
                        yield protocol_error("thread.started 缺少 thread_id", evt)
                        return
                    thread_id = tid
                    yield AgentEvent(kind=AgentEventKind.THREAD_STARTED,
                                     backend_thread_id=thread_id, raw=evt)
                elif etype == "item.completed":
                    item = evt.get("item")
                    if not isinstance(item, dict) or not item.get("type"):
                        yield protocol_error("item.completed 缺少 item.type", evt)
                        return
                    itype = item["type"]
                    if itype == "tool_call":
                        name = item.get("name")
                        if not name:
                            yield protocol_error("tool_call 缺少 name", evt)
                            return
                        yield AgentEvent(
                            kind=AgentEventKind.TOOL_CALL_COMPLETED,
                            tool=ToolEvent(name=name, input=item.get("input"),
                                           output=item.get("output")),
                            raw=evt,
                        )
                    elif itype == "agent_message":
                        if "text" not in item:
                            yield protocol_error("agent_message 缺少 text", evt)
                            return
                        final_text = item.get("text") or ""
                        yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED,
                                         text=final_text, raw=evt)
                    # 已知 item.completed 但未知 item.type → 前向兼容忽略
                # 未知顶层 type → 前向兼容忽略
            code = returncode()
        except subprocess.TimeoutExpired:
            yield timeout_error(f"codex 读取超时 ({req.timeout_s}s)")
            return
        except (OSError, subprocess.SubprocessError) as exc:
            yield crash_error(f"codex 子进程异常: {exc}")
            return
        if code != 0:
            yield crash_error(f"codex exited with {code}")
            return
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            backend_thread_id=thread_id,
            result=AgentRunResult(backend_thread_id=thread_id, final_text=final_text,
                                  usage=TokenUsage()),
        )
