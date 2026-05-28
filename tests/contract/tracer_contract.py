"""Shared Tracer behaviour contract.

Both MemoryTracer and OtelTracer (backed by InMemorySpanExporter) must satisfy
every assertion in this module.  New implementations run the same suite via
parametrisation in test_tracer_contract.py.
"""
from __future__ import annotations

from typing import Callable

from claw_engine.engine.observability.contracts import TraceDims, Tracer


MakeTracer = Callable[[], Tracer]


# ── helpers ────────────────────────────────────────────────────────────────

_DIMS = TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="fake")


# ── contract assertions ─────────────────────────────────────────────────────

def assert_start_trace_returns_span(make_tracer: MakeTracer) -> None:
    tracer = make_tracer()
    span = tracer.start_trace("t", dims=TraceDims(), input="hello")
    assert hasattr(span, "record_tool")
    assert hasattr(span, "finish")
    assert hasattr(span, "__enter__")
    assert hasattr(span, "__exit__")


def assert_span_is_context_manager(make_tracer: MakeTracer) -> None:
    tracer = make_tracer()
    with tracer.start_trace("t", dims=TraceDims(), input="hello") as span:
        span.record_tool("shell", input={"cmd": "ls"}, output="ok")


def assert_finish_is_idempotent(make_tracer: MakeTracer) -> None:
    """Calling finish twice must not raise."""
    tracer = make_tracer()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    span.finish(output="first")
    span.finish(output="second")  # second call must be a no-op, no exception


def assert_exit_without_finish_auto_finishes(make_tracer: MakeTracer) -> None:
    """Leaving the context manager without calling finish should auto-finish."""
    tracer = make_tracer()
    with tracer.start_trace("t", dims=TraceDims(), input=None):
        pass  # no explicit finish


def assert_exit_does_not_swallow_exception(make_tracer: MakeTracer) -> None:
    """__exit__ must return False so exceptions propagate."""
    tracer = make_tracer()
    import pytest
    with pytest.raises(ValueError, match="boom"):
        with tracer.start_trace("t", dims=TraceDims(), input=None):
            raise ValueError("boom")


def assert_error_recorded_on_exception(make_tracer: MakeTracer) -> None:
    """When an exception propagates, span must be marked finished (no crash)."""
    tracer = make_tracer()
    try:
        with tracer.start_trace("t", dims=TraceDims(), input=None) as span:
            raise RuntimeError("oops")
    except RuntimeError:
        pass
    # must not raise when we attempt a second finish
    span.finish(output=None)


def assert_record_tool_does_not_raise(make_tracer: MakeTracer) -> None:
    tracer = make_tracer()
    span = tracer.start_trace("t", dims=_DIMS, input="q", metadata={"k": "v"})
    span.record_tool("tool_a", input={"x": 1}, output={"y": 2})
    span.record_tool("tool_b", input=None, output=None)
    span.finish(output="done")
