"""跨后端共享的行为契约断言。codex / claude 用各自 canned 流跑同一套。"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional
from claw_engine.engine.runtime.contracts import (
    AgentEventKind, AgentErrorKind, AgentRunRequest, CodeAgentBackend,
)
from claw_engine.engine.runtime.contract_suite import assert_valid_event_stream

# make_backend(lines, returncode, raise_exc) -> backend（注入式 spawn）
MakeBackend = Callable[[Optional[list], int, Optional[BaseException]], CodeAgentBackend]


@dataclass(frozen=True)
class BackendContractFixture:
    name: str
    make_backend: MakeBackend
    success_lines: list
    expected_final_text: str
    expected_thread_id: str
    malformed_lines: list  # 含一条非 JSON 行，应触发 PROTOCOL


def _run(backend: CodeAgentBackend, **kw):
    return list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, **kw)))


def assert_success(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(fx.success_lines, 0, None))
    assert_valid_event_stream(events)
    assert events[0].kind is AgentEventKind.THREAD_STARTED
    assert events[0].backend_thread_id == fx.expected_thread_id
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == fx.expected_final_text
    assert events[-1].result.backend_thread_id == fx.expected_thread_id


def assert_nonzero_exit_is_backend_crash(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend([], 1, None))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR
    assert events[-1].error.kind is AgentErrorKind.BACKEND_CRASH


def assert_malformed_is_protocol(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(fx.malformed_lines, 0, None))
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.PROTOCOL
    assert not any(e.kind is AgentEventKind.TURN_COMPLETED for e in events)


def assert_timeout_is_error(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(None, 0, subprocess.TimeoutExpired("cli", 1)), timeout_s=1)
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.TIMEOUT


def assert_crash_is_backend_crash(fx: BackendContractFixture) -> None:
    events = _run(fx.make_backend(None, 0, OSError("broken pipe")))
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.BACKEND_CRASH
