from __future__ import annotations
import argparse
import os
import sys
from claw_engine.engine.bootstrap import EngineRegistry
from claw_engine.engine.orchestration.engine import Engine, AgentRunFailed
from claw_engine.adapters.backends.codex.backend import CodexCliBackend


def _build_registry() -> EngineRegistry:
    reg = EngineRegistry()
    reg.register_backend("codex", lambda: CodexCliBackend())
    from claw_engine.adapters.backends.claude.backend import ClaudeCodeBackend
    reg.register_backend("claude", lambda: ClaudeCodeBackend())
    # 仅用于 smoke：内置一个 echo 假后端（不依赖真实 CLI）
    from claw_engine.engine.runtime.contracts import (
        AgentEvent, AgentEventKind, AgentRunResult, TokenUsage,
        BackendCapabilities, BackendHealth,
    )

    class _EchoBackend:
        name = "fake"
        def capabilities(self):
            return BackendCapabilities(False, False, False, False, ("none",))
        def healthcheck(self):
            return BackendHealth(ok=True)
        def run(self, req):
            text = f"echo: {req.prompt}"
            yield AgentEvent(kind=AgentEventKind.MESSAGE_COMPLETED, text=text)
            yield AgentEvent(kind=AgentEventKind.TURN_COMPLETED,
                             result=AgentRunResult(None, text, TokenUsage()))

    reg.register_backend("fake", lambda: _EchoBackend())
    return reg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claw_engine")
    parser.add_argument("--backend", default="codex")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args(argv)

    engine = Engine(_build_registry())
    try:
        result = engine.run_turn(
            backend_name=args.backend, prompt=args.prompt, cwd=args.cwd, env=dict(os.environ),
        )
    except AgentRunFailed as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(result.final_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
