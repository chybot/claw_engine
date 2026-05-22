import subprocess
import sys

def test_cli_runs_one_turn_with_fake_backend():
    out = subprocess.run(
        [sys.executable, "-m", "claw_engine", "--backend", "fake", "--prompt", "hello"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "echo: hello" in out.stdout


def test_cli_registers_codex_and_claude_backends():
    from claw_engine.__main__ import _build_registry
    reg = _build_registry()
    # 解析不报错即说明已注册（构造不触发子进程/登录）
    assert reg.resolve_backend("codex").name == "codex"
    assert reg.resolve_backend("claude").name == "claude"
    assert reg.resolve_backend("fake").name == "fake"
