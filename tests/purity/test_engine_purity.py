# tests/purity/test_engine_purity.py
import ast
import pathlib

ENGINE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "claw_engine" / "engine"
FORBIDDEN_TOKENS = ("seatalk", "jira", "codex", "claude")

def _py_files(root):
    return [p for p in root.rglob("*.py")]

def test_engine_has_no_adapter_imports():
    offenders = []
    for path in _py_files(ENGINE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = ",".join(a.name for a in node.names)
            if mod and "adapters" in mod:
                offenders.append(f"{path}: imports {mod}")
    assert not offenders, "engine 不得 import adapters:\n" + "\n".join(offenders)

def test_engine_has_no_business_or_cli_tokens():
    offenders = []
    for path in _py_files(ENGINE_ROOT):
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path}: contains '{token}'")
    assert not offenders, "engine 不得含业务/渠道/CLI 字面量:\n" + "\n".join(offenders)
