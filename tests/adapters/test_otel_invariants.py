"""OtelTracer-specific invariant tests.

These go beyond the shared Tracer contract and verify OTel adapter internals:

  (a) Double-finish idempotent: span.end() called only once.
  (b) __exit__ fallback finish + never swallows exception.
  (c) Engine-provided dims / metadata attributes do NOT contain secrets.
      NOTE: record_tool payload is NOT asserted here (outside engine's redact
      contract per P6c).
  (d) Oversized input/output truncated to MAX_ATTR_LEN with truncated flag.
"""
from __future__ import annotations

import pytest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from claw_engine.engine.observability.contracts import TraceDims
from claw_engine.adapters.observability.otel import OtelTracer
from claw_engine.adapters.observability.otel.tracer import MAX_ATTR_LEN


# ── fixture helper ──────────────────────────────────────────────────────────

def _make_tracer_with_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OtelTracer(provider), exporter


# ── (a) Double-finish idempotency ───────────────────────────────────────────

def test_double_finish_is_noop():
    tracer, exporter = _make_tracer_with_exporter()
    span = tracer.start_trace("t", dims=TraceDims(), input=None)
    span.finish(output="first")
    span.finish(output="second")  # must not raise
    finished_spans = exporter.get_finished_spans()
    # Only one OTel span should be ended
    assert len(finished_spans) == 1


# ── (b) __exit__ fallback + exception propagation ──────────────────────────

def test_exit_fallback_finishes_span_on_exception():
    """span.finish() must be called by __exit__ when not done manually."""
    tracer, exporter = _make_tracer_with_exporter()
    try:
        with tracer.start_trace("t", dims=TraceDims(), input=None):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert len(exporter.get_finished_spans()) == 1


def test_exit_does_not_swallow_exception():
    """__exit__ must return False so the exception propagates."""
    tracer, _ = _make_tracer_with_exporter()
    with pytest.raises(ValueError, match="sentinel"):
        with tracer.start_trace("t", dims=TraceDims(), input=None):
            raise ValueError("sentinel")


def test_exit_without_exception_finishes_cleanly():
    tracer, exporter = _make_tracer_with_exporter()
    with tracer.start_trace("t", dims=TraceDims(), input=None):
        pass  # no explicit finish, no exception
    assert len(exporter.get_finished_spans()) == 1


# ── (c) Engine-provided dims / metadata never contain secrets ──────────────

def test_dims_attributes_do_not_contain_secrets():
    """The 4 standard dim attributes come from engine-redacted TraceDims.

    Negative example: inject a 'secret' value into metadata.  The 4 dim
    OTel attributes (claw.workspace_id etc.) must never reflect it.
    """
    tracer, exporter = _make_tracer_with_exporter()
    dims = TraceDims(
        workspace_id="ws1",
        user_id="u1",
        session_id="s1",
        backend_name="codex",
    )
    # metadata contains a secret — engine already separates this from dims, but
    # we confirm the adapter does NOT forward metadata values into dim attributes
    metadata = {"redacted_env": {"TOKEN": "***"}, "injected_secret": "super-secret-value"}

    with tracer.start_trace("t", dims=dims, input="hello", metadata=metadata) as span:
        span.finish(output="done")

    finished = exporter.get_finished_spans()
    assert finished, "span should be ended"
    attrs = dict(finished[0].attributes or {})

    # 4 standard dim attributes must be exactly the dim values
    assert attrs.get("claw.workspace_id") == "ws1"
    assert attrs.get("claw.user_id") == "u1"
    assert attrs.get("claw.session_id") == "s1"
    assert attrs.get("claw.backend_name") == "codex"

    # The raw secret value must NOT appear in any dim attribute
    raw_attrs_str = repr(attrs)
    assert "super-secret-value" not in raw_attrs_str, (
        "dim attributes leaked a secret from metadata"
    )


def test_dims_attributes_absent_when_none():
    """None-valued dims must be omitted from OTel attributes."""
    tracer, exporter = _make_tracer_with_exporter()
    dims = TraceDims()  # all None
    with tracer.start_trace("t", dims=dims, input=None) as span:
        span.finish()
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert "claw.workspace_id" not in attrs
    assert "claw.user_id" not in attrs
    assert "claw.session_id" not in attrs
    assert "claw.backend_name" not in attrs


# ── (d) Truncation of oversized tool input/output ──────────────────────────

def test_large_input_truncated_with_flag():
    """record_tool input exceeding MAX_ATTR_LEN must be truncated + flagged."""
    tracer, exporter = _make_tracer_with_exporter()
    big_input = {"data": "x" * (MAX_ATTR_LEN + 500)}

    with tracer.start_trace("t", dims=TraceDims(), input=None) as span:
        span.record_tool("big_tool", input=big_input, output="small")
        span.finish()

    events = exporter.get_finished_spans()[0].events
    assert events, "expected tool events"
    event_attrs = dict(events[0].attributes or {})

    input_val = event_attrs.get("tool.input", "")
    assert len(input_val) <= MAX_ATTR_LEN, "input was not truncated"
    assert event_attrs.get("tool.input.truncated") is True, "truncated flag missing"
    # output was small, no truncation flag
    assert event_attrs.get("tool.output.truncated") is not True


def test_large_output_truncated_with_flag():
    """record_tool output exceeding MAX_ATTR_LEN must be truncated + flagged."""
    tracer, exporter = _make_tracer_with_exporter()
    big_output = "y" * (MAX_ATTR_LEN + 100)

    with tracer.start_trace("t", dims=TraceDims(), input=None) as span:
        span.record_tool("big_tool", input="small", output=big_output)
        span.finish()

    events = exporter.get_finished_spans()[0].events
    event_attrs = dict(events[0].attributes or {})

    output_val = event_attrs.get("tool.output", "")
    assert len(output_val) <= MAX_ATTR_LEN
    assert event_attrs.get("tool.output.truncated") is True
    assert event_attrs.get("tool.input.truncated") is not True


def test_non_scalar_input_serialized_as_json():
    """Non-string tool input must be JSON-serialised without error."""
    tracer, exporter = _make_tracer_with_exporter()
    with tracer.start_trace("t", dims=TraceDims(), input=None) as span:
        span.record_tool("t", input={"key": [1, 2, 3]}, output=42)
        span.finish()
    events = exporter.get_finished_spans()[0].events
    event_attrs = dict(events[0].attributes or {})
    # Must be a string (serialised)
    assert isinstance(event_attrs.get("tool.input"), str)
    assert isinstance(event_attrs.get("tool.output"), str)


def test_unserializable_input_falls_back_to_repr():
    """json.dumps failure must fall back to repr(), never raise."""
    tracer, exporter = _make_tracer_with_exporter()

    class Unserializable:
        def __repr__(self):
            return "<Unserializable>"

    with tracer.start_trace("t", dims=TraceDims(), input=None) as span:
        span.record_tool("t", input=Unserializable(), output=None)
        span.finish()
    # Should not raise; event must exist
    events = exporter.get_finished_spans()[0].events
    assert events
