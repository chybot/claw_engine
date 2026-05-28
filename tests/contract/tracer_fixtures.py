"""Tracer factory + inspector fixtures used by the contract suite.

OTel imports are top-level: importers (test files) must guard with
``pytest.importorskip("opentelemetry.sdk")`` before importing this module.
"""
from __future__ import annotations

from typing import Any, List, Tuple

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.adapters.observability.otel import OtelTracer
from claw_engine.adapters.observability.otel.tracer import OtelTraceSpan

from tests.contract.tracer_contract import TracerInspector


# ── MemoryTracer inspector ─────────────────────────────────────────────────

class _MemoryInspector(TracerInspector):
    """Inspector for MemoryTracer (reads span._record directly)."""

    def is_finished(self, span: Any) -> bool:
        return bool(span._record.finished)

    def finished_count(self, span: Any) -> int:
        # MemorySpan.finish() guards with an internal flag, so the underlying
        # end is invoked at most once regardless of how often caller calls it.
        return 1 if span._record.finished else 0

    def tool_events(self, span: Any) -> List[dict]:
        return [{"name": t["name"], "input": t["input"], "output": t["output"]}
                for t in span._record.tools]

    def status(self, span: Any) -> str:
        if not span._record.finished:
            return "UNSET"
        return "ERROR" if span._record.error is not None else "OK"


def make_memory_tracer() -> Tuple[MemoryTracer, TracerInspector]:
    return MemoryTracer(), _MemoryInspector()


# ── OtelTracer inspector ───────────────────────────────────────────────────

class _OtelInspector(TracerInspector):
    """Inspector for OtelTracer.

    Bridges the user-facing ``OtelTraceSpan`` to the underlying OTel
    ``ReadableSpan`` collected by ``InMemorySpanExporter``.
    """

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    def _readable_for(self, span: OtelTraceSpan):
        """Return the ReadableSpan matching `span._span`, or None if not yet ended."""
        target_ctx = span._span.get_span_context()
        for finished in self._exporter.get_finished_spans():
            if finished.get_span_context().span_id == target_ctx.span_id:
                return finished
        return None

    def is_finished(self, span: Any) -> bool:
        # OtelTraceSpan flips _ended atomically with span.end()
        return bool(span._ended)

    def finished_count(self, span: Any) -> int:
        readable = self._readable_for(span)
        if readable is None:
            return 0
        # Exporter receives one ReadableSpan per end() call — count matches.
        target = readable.get_span_context().span_id
        return sum(
            1 for s in self._exporter.get_finished_spans()
            if s.get_span_context().span_id == target
        )

    def tool_events(self, span: Any) -> List[dict]:
        readable = self._readable_for(span)
        if readable is None:
            return []
        out: List[dict] = []
        for ev in readable.events:
            attrs = dict(ev.attributes or {})
            out.append({
                "name": attrs.get("tool.name", ev.name),
                "attrs": attrs,
            })
        return out

    def status(self, span: Any) -> str:
        readable = self._readable_for(span)
        if readable is None:
            return "UNSET"
        code = readable.status.status_code
        # opentelemetry.trace.StatusCode: OK / ERROR / UNSET
        return code.name


def make_otel_tracer() -> Tuple[OtelTracer, TracerInspector]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OtelTracer(provider), _OtelInspector(exporter)


def make_otel_tracer_with_exporter() -> Tuple[OtelTracer, InMemorySpanExporter]:
    """Convenience: build an OtelTracer wired to an InMemorySpanExporter.

    Used by invariant tests that need direct exporter access for attribute
    inspection (more granular than the inspector interface exposes).
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OtelTracer(provider), exporter
