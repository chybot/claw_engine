"""OpenTelemetry Tracer adapter for claw_engine.

Implements the engine's Tracer / TraceSpan Protocol using the OTel API.

Design decisions:
- Uses ``tracer.start_span(name)`` (NOT ``start_as_current_span``) to avoid the
  context-manager auto-end racing with manual ``finish()`` calls.
- ``OtelTraceSpan._ended`` guards idempotent ``span.end()`` — OTel spans are
  one-shot; calling end() twice on an OTel span is undefined.
- ``__exit__`` always returns False to propagate exceptions.
- ``record_tool`` serialises non-native-OTel values to JSON (with repr fallback)
  and truncates to MAX_ATTR_LEN characters, setting a ``*.truncated`` flag.
- Only the 4 engine-provided TraceDims are written as span attributes; caller-
  supplied metadata is NOT forwarded to avoid leaking backend/secret data.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from opentelemetry.trace import Span, StatusCode, TracerProvider

from claw_engine.engine.observability.contracts import TraceDims

# Maximum character length for serialised tool input/output attributes.
MAX_ATTR_LEN: int = 4096

# OTel-native scalar types that can be stored as attributes directly.
_OTEL_SCALARS = (str, int, float, bool)

# Mapping from TraceDims field name → OTel attribute key.
_DIM_ATTRS: dict[str, str] = {
    "workspace_id": "claw.workspace_id",
    "user_id": "claw.user_id",
    "session_id": "claw.session_id",
    "backend_name": "claw.backend_name",
}


def _serialise(value: Any) -> tuple[str, bool]:
    """Serialise a value to a string suitable for an OTel attribute.

    Returns ``(serialised_str, was_truncated)``.

    Strategy:
    1. If already a native OTel scalar — convert to str and check length.
    2. Otherwise json.dumps(default=str) to handle most types.
    3. If json.dumps fails — fall back to repr().
    4. Truncate to MAX_ATTR_LEN, never raise.
    """
    if isinstance(value, _OTEL_SCALARS):
        text = str(value)
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = repr(value)

    if len(text) > MAX_ATTR_LEN:
        return text[:MAX_ATTR_LEN], True
    return text, False


class OtelTraceSpan:
    """Wraps an OTel ``Span`` to satisfy the engine ``TraceSpan`` Protocol."""

    def __init__(self, span: Span) -> None:
        self._span = span
        self._ended = False

    # ── TraceSpan protocol ──────────────────────────────────────────────────

    def record_tool(self, name: str, *, input: Any = None, output: Any = None) -> None:
        """Append a tool-call event to the span."""
        attrs: dict[str, Any] = {"tool.name": name}

        input_str, input_truncated = _serialise(input)
        attrs["tool.input"] = input_str
        if input_truncated:
            attrs["tool.input.truncated"] = True

        output_str, output_truncated = _serialise(output)
        attrs["tool.output"] = output_str
        if output_truncated:
            attrs["tool.output.truncated"] = True

        self._span.add_event("tool", attributes=attrs)

    def finish(self, *, output: Any = None, error: Optional[str] = None) -> None:
        """Finish the span.  Idempotent: second call is a no-op."""
        if self._ended:
            return
        self._ended = True

        if error is not None:
            self._span.set_status(StatusCode.ERROR, error)
        else:
            self._span.set_status(StatusCode.OK)

        self._span.end()

    def __enter__(self) -> "OtelTraceSpan":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if not self._ended:
            self.finish(error=str(exc) if exc is not None else None)
        # Always return False — never swallow exceptions.
        return False


class OtelTracer:
    """Tracer adapter backed by an OpenTelemetry ``TracerProvider``.

    Usage::

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from claw_engine.adapters.observability.otel import OtelTracer

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = OtelTracer(provider)

    For production wire-up substitute the real exporter (OTLP, Jaeger, etc.).
    """

    def __init__(
        self,
        provider: TracerProvider,
        service_name: str = "claw_engine",
    ) -> None:
        self._otel_tracer = provider.get_tracer(service_name)

    # ── Tracer protocol ─────────────────────────────────────────────────────

    def start_trace(
        self,
        name: str,
        *,
        dims: TraceDims,
        input: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> OtelTraceSpan:
        """Start a new trace span.

        Only the 4 engine-provided ``TraceDims`` fields are written as span
        attributes.  ``metadata`` is intentionally NOT forwarded to OTel
        attributes to avoid leaking backend metadata or secrets.
        """
        # Build dim attributes — omit None values.
        dim_attrs: dict[str, str] = {
            otel_key: str(value)
            for field_name, otel_key in _DIM_ATTRS.items()
            if (value := getattr(dims, field_name)) is not None
        }

        # start_span (not start_as_current_span) to avoid context-manager
        # auto-end colliding with our manual finish() guard.
        span: Span = self._otel_tracer.start_span(name, attributes=dim_attrs)
        return OtelTraceSpan(span)
