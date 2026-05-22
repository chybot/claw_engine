from claw_engine.engine.context.config import LayeredConfigProvider


def test_global_only_when_no_workspace_override():
    cp = LayeredConfigProvider(global_env={"A": "1", "B": "2"})
    assert dict(cp.get_env("ws1")) == {"A": "1", "B": "2"}


def test_workspace_overrides_global():
    cp = LayeredConfigProvider(
        global_env={"A": "1", "B": "2"},
        workspace_env={"ws1": {"B": "20", "C": "3"}},
    )
    assert dict(cp.get_env("ws1")) == {"A": "1", "B": "20", "C": "3"}   # workspace wins on B
    assert dict(cp.get_env("other")) == {"A": "1", "B": "2"}            # 未知 workspace 只 global


def test_returns_copy_not_internal_state():
    cp = LayeredConfigProvider(global_env={"A": "1"})
    env = cp.get_env("ws1")
    dict(env)["A"] = "mutated"
    assert dict(cp.get_env("ws1")) == {"A": "1"}                        # 内部不被外部改动污染
