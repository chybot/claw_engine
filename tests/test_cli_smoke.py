import subprocess
import sys

def test_cli_runs_one_turn_with_fake_backend():
    out = subprocess.run(
        [sys.executable, "-m", "claw_engine", "--backend", "fake", "--prompt", "hello"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "echo: hello" in out.stdout
