from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind

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
