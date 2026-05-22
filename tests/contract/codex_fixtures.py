from __future__ import annotations
from typing import Optional
from claw_engine.adapters.backends.codex.backend import CodexCliBackend
from tests.contract.backend_contract import BackendContractFixture


class _RaisingIter:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __iter__(self) -> "_RaisingIter":
        return self

    def __next__(self) -> str:
        raise self._exc


def _make(lines: Optional[list], returncode: int, raise_exc: Optional[BaseException]) -> CodexCliBackend:
    def spawn(argv, cwd, env, timeout_s):
        it = _RaisingIter(raise_exc) if raise_exc is not None else iter(lines or [])
        return it, (lambda: returncode)
    return CodexCliBackend(spawn=spawn)


SUCCESS_LINES = [
    '{"type":"thread.started","thread_id":"th_123"}',
    '{"type":"item.completed","item":{"type":"tool_call","name":"shell","input":{"cmd":"ls"}}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}',
]
MALFORMED_LINES = [
    '{"type":"thread.started","thread_id":"th_1"}',
    'not json at all {{{',
    '{"type":"item.completed","item":{"type":"agent_message","text":"unreachable"}}',
]

FIXTURE = BackendContractFixture(
    name="codex", make_backend=_make,
    success_lines=SUCCESS_LINES, expected_final_text="final answer", expected_thread_id="th_123",
    malformed_lines=MALFORMED_LINES,
)
