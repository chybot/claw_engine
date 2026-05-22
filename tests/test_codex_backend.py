import subprocess

from claw_engine.adapters.backends.codex.backend import CodexCliBackend
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind, AgentErrorKind
from claw_engine.engine.runtime.contract_suite import assert_valid_event_stream

CANNED_OK = [
    '{"type":"thread.started","thread_id":"th_123"}',
    '{"type":"item.completed","item":{"type":"tool_call","name":"shell","input":{"cmd":"ls"}}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}',
]

def _spawn_factory(lines, returncode=0):
    def spawn(argv, cwd, env, timeout_s):
        return iter(lines), (lambda: returncode)
    return spawn

def test_codex_success_normalizes_to_contract():
    backend = CodexCliBackend(spawn=_spawn_factory(CANNED_OK))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[0].kind is AgentEventKind.THREAD_STARTED
    assert events[0].backend_thread_id == "th_123"
    assert any(e.kind is AgentEventKind.TOOL_CALL_COMPLETED and e.tool.name == "shell" for e in events)
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "final answer"
    assert events[-1].result.backend_thread_id == "th_123"

def test_codex_nonzero_exit_maps_to_error():
    backend = CodexCliBackend(spawn=_spawn_factory([], returncode=1))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR

def test_codex_resume_passes_thread_id_in_argv():
    captured = {}
    def spawn(argv, cwd, env, timeout_s):
        captured["argv"] = argv
        return iter(CANNED_OK), (lambda: 0)
    backend = CodexCliBackend(spawn=spawn)
    list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, backend_thread_id="th_9")))
    assert "resume" in captured["argv"]
    assert "th_9" in captured["argv"]


# --- contract hardening: PROTOCOL detection ---

CANNED_MALFORMED = [
    '{"type":"thread.started","thread_id":"th_1"}',
    'not json at all {{{',
    '{"type":"item.completed","item":{"type":"agent_message","text":"unreachable"}}',
]

def test_codex_malformed_json_maps_to_protocol_error():
    backend = CodexCliBackend(spawn=_spawn_factory(CANNED_MALFORMED))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR
    assert events[-1].error.kind is AgentErrorKind.PROTOCOL
    # malformed 之后立即终止，不得降级成功终态
    assert not any(e.kind is AgentEventKind.TURN_COMPLETED for e in events)

def test_codex_thread_started_missing_thread_id_is_protocol_error():
    backend = CodexCliBackend(spawn=_spawn_factory(['{"type":"thread.started"}']))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.PROTOCOL

def test_codex_item_completed_missing_type_is_protocol_error():
    lines = [
        '{"type":"thread.started","thread_id":"th_1"}',
        '{"type":"item.completed","item":{"name":"shell"}}',
    ]
    backend = CodexCliBackend(spawn=_spawn_factory(lines))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].error.kind is AgentErrorKind.PROTOCOL

def test_codex_unknown_type_is_ignored_forward_compat():
    lines = [
        '{"type":"thread.started","thread_id":"th_1"}',
        '{"type":"some.future.event","payload":123}',
        '{"type":"item.completed","item":{"type":"reasoning","text":"thinking"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
    ]
    backend = CodexCliBackend(spawn=_spawn_factory(lines))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "done"


# --- contract hardening: timeout / subprocess exception normalization ---

class _RaisingIter:
    """读 stdout 阶段抛异常的迭代器（模拟超时/子进程崩溃）。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __iter__(self) -> "_RaisingIter":
        return self

    def __next__(self) -> str:
        raise self._exc

def test_codex_timeout_maps_to_error():
    def spawn(argv, cwd, env, timeout_s):
        return _RaisingIter(subprocess.TimeoutExpired(argv, timeout_s)), (lambda: 0)
    backend = CodexCliBackend(spawn=spawn)
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, timeout_s=1)))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR
    assert events[-1].error.kind is AgentErrorKind.TIMEOUT

def test_codex_subprocess_exception_maps_to_backend_crash():
    def spawn(argv, cwd, env, timeout_s):
        return _RaisingIter(OSError("broken pipe")), (lambda: 0)
    backend = CodexCliBackend(spawn=spawn)
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert_valid_event_stream(events)
    assert events[-1].kind is AgentEventKind.ERROR
    assert events[-1].error.kind is AgentErrorKind.BACKEND_CRASH
