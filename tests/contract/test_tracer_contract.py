"""Parametrised Tracer contract tests.

Runs every behaviour assertion in tracer_contract.py against:
  - MemoryTracer  (engine built-in)
  - OtelTracer    (adapter, backed by InMemorySpanExporter)
"""
from __future__ import annotations

import pytest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from claw_engine.engine.observability.memory import MemoryTracer
from claw_engine.adapters.observability.otel import OtelTracer
from tests.contract import tracer_contract as tc


def _make_memory_tracer():
    return MemoryTracer()


def _make_otel_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OtelTracer(provider)


TRACERS = [
    ("memory", _make_memory_tracer),
    ("otel", _make_otel_tracer),
]


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_start_trace_returns_span(name, make_tracer):
    tc.assert_start_trace_returns_span(make_tracer)


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_span_is_context_manager(name, make_tracer):
    tc.assert_span_is_context_manager(make_tracer)


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_finish_is_idempotent(name, make_tracer):
    tc.assert_finish_is_idempotent(make_tracer)


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_exit_without_finish_auto_finishes(name, make_tracer):
    tc.assert_exit_without_finish_auto_finishes(make_tracer)


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_exit_does_not_swallow_exception(name, make_tracer):
    tc.assert_exit_does_not_swallow_exception(make_tracer)


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_error_recorded_on_exception(name, make_tracer):
    tc.assert_error_recorded_on_exception(make_tracer)


@pytest.mark.parametrize("name,make_tracer", TRACERS, ids=[n for n, _ in TRACERS])
def test_record_tool_does_not_raise(name, make_tracer):
    tc.assert_record_tool_does_not_raise(make_tracer)
