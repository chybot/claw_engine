from claw_engine.adapters.backends.codex.backend import CodexCliBackend
from claw_engine.engine.runtime.contracts import AgentRunRequest, AgentEventKind
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
