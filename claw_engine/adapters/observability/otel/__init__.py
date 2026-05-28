"""OTel (OpenTelemetry) Tracer adapter package.

See :mod:`claw_engine.adapters.observability.otel.tracer` for design decisions:
attribute naming (``claw.*``), serialisation strategy (JSON → repr → sentinel),
span lifecycle (``start_span`` + manual end with ``_ended`` guard), and the
redact-safety invariant (metadata is NOT forwarded to span attributes).
"""
from claw_engine.adapters.observability.otel.tracer import OtelTracer

__all__ = ["OtelTracer"]
