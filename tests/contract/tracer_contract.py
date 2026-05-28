"""Shared Tracer behaviour contract.

Both MemoryTracer and OtelTracer (backed by InMemorySpanExporter) must satisfy
every assertion in this module.  New implementations run the same suite via
parametrisation in test_tracer_contract.py.

Factory contract
----------------
Each implementation provides a zero-arg factory returning ``(tracer, inspector)``.
The ``inspector`` exposes observable post-conditions about spans returned by
the tracer:

    - is_finished(span) -> bool           # was finish()/end() called?
    - finished_count(span) -> int         # number of underlying end() calls
                                          # (used to verify idempotency)
    - tool_events(span) -> list[dict]     # tool events recorded on the span
                                          # each dict has at minimum a "name" key
    - status(span) -> str                 # "OK" / "ERROR" / "UNSET"

This lets a future inert / no-op adapter fail the contract: weak "did not raise"
checks would let it slip through, but observable assertions catch silent drops.
"""
from __future__ import annotations

from typing import Any, Callable, List, Tuple

from claw_engine.engine.observability.contracts import TraceDims, Tracer


class TracerInspector:
    """Adapter-agnostic introspection of span state."""

    def is_finished(self, span: Any) -> bool:  # pragma: no cover - interface only
        raise NotImplementedError

    def finished_count(self, span: Any) -> int:  # pragma: no cover - interface only
        raise NotImplementedError

    def tool_events(self, span: Any) -> List[dict]:  # pragma: no cover - interface only
        raise NotImplementedError

    def status(self, span: Any) -> str:  # pragma: no cover - interface only
        raise NotImplementedError


MakeTracerInspector = Callable[[], Tuple[Tracer, TracerInspector]]


# ── helpers ────────────────────────────────────────────────────────────────

_DIMS = TraceDims(workspace_id="ws1", user_id="u1", session_id="s1", backend_name="fake")


# ── contract assertions ─────────────────────────────────────────────────────

def assert_start_trace_returns_span(make: MakeTracerInspector) -> None:
    tracer, _ = make()
    span = tracer.start_trace("t", dims=TraceDims(), input="hello")
    assert hasattr(span, "record_tool")
    assert hasattr(span, "finish")
    assert hasattr(span, "__enter__")
    assert hasattr(span, "__exit__")


def assert_span_is_context_manager_and_finishes(make: MakeTracerInspector) -> None:
    """Using `with` must produce an observably finished span."""
    tracer, inspector = make()
    span = tracer.start_trace("t", dims=TraceDims(), input="hello")
    with span:
        span.record_tool("shell", input={"cmd": "ls"}, output="ok")
    assert inspector.is_finished(span) is True
    assert inspector.finished_count(span) == 1


def assert_finish_is_idempotent(make: MakeTracerInspector) -> None:
    """Calling finish twice must not raise AND must end only once."""
    tracer, inspector = make()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    span.finish(output="first")
    span.finish(output="second")  # second call must be a no-op
    assert inspector.is_finished(span) is True
    assert inspector.finished_count(span) == 1


def assert_exit_without_finish_auto_finishes(make: MakeTracerInspector) -> None:
    """Leaving the context manager without calling finish should auto-finish."""
    tracer, inspector = make()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    with span:
        pass  # no explicit finish
    assert inspector.is_finished(span) is True


def assert_exit_does_not_swallow_exception(make: MakeTracerInspector) -> None:
    """__exit__ must return False so exceptions propagate."""
    tracer, inspector = make()
    import pytest

    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    with pytest.raises(ValueError, match="boom"):
        with span:
            raise ValueError("boom")
    # span must still be finished after exception
    assert inspector.is_finished(span) is True
    # status should reflect the error
    assert inspector.status(span) == "ERROR"


def assert_record_tool_visible_in_events(make: MakeTracerInspector) -> None:
    """record_tool must produce an observable event with the given name."""
    tracer, inspector = make()
    span = tracer.start_trace("t", dims=_DIMS, input="q", metadata={"k": "v"})
    span.record_tool("tool_a", input={"x": 1}, output={"y": 2})
    span.record_tool("tool_b")
    span.finish(output="done")

    events = inspector.tool_events(span)
    names = [e["name"] for e in events]
    assert "tool_a" in names
    assert "tool_b" in names


def assert_success_status_after_clean_finish(make: MakeTracerInspector) -> None:
    """finish() without error should mark status OK."""
    tracer, inspector = make()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    span.finish(output="ok")
    assert inspector.status(span) == "OK"
