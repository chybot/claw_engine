from claw_engine.engine.context.secrets import InMemorySecretProvider, redact

def test_get_secrets_per_workspace():
    sp = InMemorySecretProvider({"ws1": {"TOKEN": "t-123"}})
    assert dict(sp.get_secrets("ws1")) == {"TOKEN": "t-123"}
    assert dict(sp.get_secrets("other")) == {}

def test_redact_masks_only_sensitive_keys():
    env = {"A": "1", "TOKEN": "t-123", "COOKIE": "c-xyz"}
    out = redact(env, frozenset({"TOKEN", "COOKIE"}))
    assert out == {"A": "1", "TOKEN": "***", "COOKIE": "***"}   # 非敏感不动，敏感打码

def test_redact_no_sensitive_keys_is_passthrough():
    env = {"A": "1"}
    assert redact(env, frozenset()) == {"A": "1"}
