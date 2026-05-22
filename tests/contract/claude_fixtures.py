from __future__ import annotations
from typing import Optional
from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
from tests.contract.backend_contract import BackendContractFixture


class _RaisingIter:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __iter__(self) -> "_RaisingIter":
        return self

    def __next__(self) -> str:
        raise self._exc


def _make(lines: Optional[list], returncode: int, raise_exc: Optional[BaseException]) -> ClaudeCodeBackend:
    def spawn(argv, cwd, env, timeout_s):
        it = _RaisingIter(raise_exc) if raise_exc is not None else iter(lines or [])
        return it, (lambda: returncode)
    return ClaudeCodeBackend(spawn=spawn)


SUCCESS_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_abc","model":"claude-x"}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"final answer"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"final answer","session_id":"sess_abc"}',
]
MALFORMED_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_abc"}',
    'not json at all {{{',
    '{"type":"result","subtype":"success","is_error":false,"result":"unreachable","session_id":"sess_abc"}',
]

FIXTURE = BackendContractFixture(
    name="claude", make_backend=_make,
    success_lines=SUCCESS_LINES, expected_final_text="final answer", expected_thread_id="sess_abc",
    malformed_lines=MALFORMED_LINES,
)
