from __future__ import annotations
import json
import shutil
import subprocess
from typing import Iterator, Optional
from claw_engine.engine.runtime.contracts import (
    AgentEvent, AgentEventKind, AgentRunRequest, AgentRunResult,
    BackendCapabilities, BackendHealth, ToolEvent, TokenUsage,
)
from claw_engine.adapters.backends._subprocess import SpawnFn, default_spawn
from claw_engine.adapters.backends._errors import protocol_error, timeout_error, crash_error


class ClaudeCodeBackend:
    name = "claude"

    def __init__(self, command: str = "claude", spawn: Optional[SpawnFn] = None) -> None:
        self._command = command
        self._spawn = spawn or default_spawn

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_resume=True, supports_streaming=False,
            supports_tools=True, supports_mcp=False, auth_modes=("api_key", "cli_login"),
        )

    def healthcheck(self) -> BackendHealth:
        ok = shutil.which(self._command) is not None
        return BackendHealth(ok=ok, detail="" if ok else "claude not on PATH")

    def _build_argv(self, req: AgentRunRequest) -> list:
        argv = [self._command, "-p", req.prompt,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions"]
        if req.backend_thread_id:
            argv += ["--resume", req.backend_thread_id]
        if req.model:
            argv += ["--model", req.model]
        return argv

    def run(self, req: AgentRunRequest) -> Iterator[AgentEvent]:
        argv = self._build_argv(req)
        line_iter, returncode = self._spawn(argv, req.cwd, req.env, req.timeout_s)
        session_id = req.backend_thread_id
        final_text = ""
        try:
            for raw_line in line_iter:
                if not raw_line.strip():
                    continue
                try:
                    evt = json.loads(raw_line)
                except json.JSONDecodeError:
                    yield protocol_error(f"无法解析 claude 输出行: {raw_line[:200]!r}")
                    return
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "init":
                    sid = evt.get("session_id")
                    if not sid:
                        yield protocol_error("system/init 缺少 session_id", evt)
                        return
                    session_id = sid
                    yield AgentEvent(kind=AgentEventKind.THREAD_STARTED,
                                     backend_thread_id=session_id, raw=evt)
                elif etype == "assistant":
                    message = evt.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        yield protocol_error("assistant 缺少 message.content", evt)
                        return
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            if "text" not in block:
                                yield protocol_error("text block 缺少 text", evt)
                                return
                            final_text = block.get("text") or ""
                            yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED,
                                             text=final_text, raw=evt)
                        elif btype == "tool_use":
                            name = block.get("name")
                            if not name:
                                yield protocol_error("tool_use 缺少 name", evt)
                                return
                            yield AgentEvent(
                                kind=AgentEventKind.TOOL_CALL_COMPLETED,
                                tool=ToolEvent(name=name, input=block.get("input")),
                                raw=evt,
                            )
                        # 未知 block.type → 前向兼容忽略
                elif etype == "result":
                    if evt.get("is_error"):
                        yield crash_error(f"claude result error: {evt.get('subtype', 'unknown')}")
                        return
                    if "result" not in evt:
                        yield protocol_error("result(success) 缺少 result 文本", evt)
                        return
                    rsid = evt.get("session_id")
                    if not rsid:
                        yield protocol_error("result 缺少 session_id", evt)
                        return
                    if session_id is not None and rsid != session_id:
                        yield protocol_error(
                            f"result.session_id 与 init 不一致: {rsid!r} != {session_id!r}", evt)
                        return
                    session_id = rsid
                    final_text = evt.get("result") or final_text
                    usage = evt.get("usage") or {}
                    yield AgentEvent(
                        kind=AgentEventKind.TURN_COMPLETED,
                        backend_thread_id=session_id,
                        result=AgentRunResult(
                            backend_thread_id=session_id, final_text=final_text,
                            usage=TokenUsage(
                                input_tokens=int(usage.get("input_tokens", 0) or 0),
                                output_tokens=int(usage.get("output_tokens", 0) or 0),
                            ),
                        ),
                    )
                    return
                # 未知顶层 type / user(tool_result) → 前向兼容忽略
            code = returncode()
        except subprocess.TimeoutExpired:
            yield timeout_error(f"claude 读取超时 ({req.timeout_s}s)")
            return
        except (OSError, subprocess.SubprocessError) as exc:
            yield crash_error(f"claude 子进程异常: {exc}")
            return
        # 走到这里说明流结束但没有 result 终态
        if code != 0:
            yield crash_error(f"claude exited with {code}")
            return
        yield protocol_error("claude 流结束但缺少 result 终态事件")
