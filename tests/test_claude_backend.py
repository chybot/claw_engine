from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind, AgentErrorKind

SUCCESS_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_abc","model":"claude-x"}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"ls"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tu1","content":"file.txt"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"final answer"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"final answer","session_id":"sess_abc","usage":{"input_tokens":100,"output_tokens":20}}',
]


def _spawn_factory(lines, returncode=0):
    def spawn(argv, cwd, env, timeout_s):
        return iter(lines), (lambda: returncode)
    return spawn


def test_claude_success_maps_to_contract():
    backend = ClaudeCodeBackend(spawn=_spawn_factory(SUCCESS_LINES))
    events = list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))
    assert events[0].kind is AgentEventKind.THREAD_STARTED
    assert events[0].backend_thread_id == "sess_abc"
    assert any(e.kind is AgentEventKind.TOOL_CALL_COMPLETED and e.tool.name == "Bash" for e in events)
    assert events[-1].kind is AgentEventKind.TURN_COMPLETED
    assert events[-1].result.final_text == "final answer"
    assert events[-1].result.backend_thread_id == "sess_abc"


def test_claude_resume_passes_session_id_in_argv():
    captured = {}

    def spawn(argv, cwd, env, timeout_s):
        captured["argv"] = argv
        return iter(SUCCESS_LINES), (lambda: 0)

    backend = ClaudeCodeBackend(spawn=spawn)
    list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={}, backend_thread_id="sess_9")))
    assert "--resume" in captured["argv"]
    assert "sess_9" in captured["argv"]
    assert "--output-format" in captured["argv"] and "stream-json" in captured["argv"]


def test_claude_capabilities_are_honest():
    caps = ClaudeCodeBackend().capabilities()
    assert caps.supports_resume is True
    assert caps.supports_streaming is False
    assert caps.supports_tools is True
    assert caps.supports_mcp is False   # V1 不接 --mcp-config，不虚标


# ---------------------------------------------------------------------------
# Task 4: behavior-lock (schema-guard regression tests)
# ---------------------------------------------------------------------------


def _events(lines):
    backend = ClaudeCodeBackend(spawn=_spawn_factory(lines))
    return list(backend.run(AgentRunRequest(prompt="hi", cwd="/tmp", env={})))


def test_claude_init_missing_session_id_is_protocol():
    ev = _events(['{"type":"system","subtype":"init","model":"x"}'])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL


def test_claude_assistant_missing_content_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{}}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL


def test_claude_tool_use_missing_name_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t","input":{}}]}}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL


def test_claude_result_error_maps_to_backend_crash():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"s1"}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.BACKEND_CRASH


def test_claude_unknown_type_is_ignored_forward_compat():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"some_future_event","x":1}',
        '{"type":"assistant","message":{"content":[{"type":"thinking","text":"hmm"}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","session_id":"s1"}',
    ])
    assert ev[-1].kind is AgentEventKind.TURN_COMPLETED
    assert ev[-1].result.final_text == "done"


def test_claude_stream_without_result_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL


def test_claude_result_missing_session_id_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done"}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL


def test_claude_result_session_id_mismatch_is_protocol():
    ev = _events([
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","session_id":"s2"}',
    ])
    assert ev[-1].error.kind is AgentErrorKind.PROTOCOL
