"""Parametrised Tracer contract tests.

Runs every behaviour assertion in tracer_contract.py against:
  - MemoryTracer  (engine built-in)
  - OtelTracer    (adapter, backed by InMemorySpanExporter)

The whole module is gated on the OTel SDK being installed (it would otherwise
fail collection).  Users without the ``[otel]`` extra still get the
MemoryTracer contract coverage via the existing engine-side tests in
``tests/test_memory_tracer.py`` and ``tests/test_tracer_contracts.py``.
"""
from __future__ import annotations

import pytest

# Gate the entire module on the OTel SDK so collection does not fail when
# the `[otel]` extra is not installed.  Must run BEFORE importing OTel-backed
# fixtures.
pytest.importorskip(
    "opentelemetry.sdk",
    reason="OTel SDK not installed (install with `pip install -e .[otel]`)",
)

from tests.contract import tracer_contract as tc  # noqa: E402
from tests.contract.tracer_fixtures import (  # noqa: E402
    make_memory_tracer,
    make_otel_tracer,
)


TRACERS = [
    ("memory", make_memory_tracer),
    ("otel", make_otel_tracer),
]


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_start_trace_returns_span(name, make):
    tc.assert_start_trace_returns_span(make)


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_span_is_context_manager_and_finishes(name, make):
    tc.assert_span_is_context_manager_and_finishes(make)


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_finish_is_idempotent(name, make):
    tc.assert_finish_is_idempotent(make)


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_exit_without_finish_auto_finishes(name, make):
    tc.assert_exit_without_finish_auto_finishes(make)


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_exit_does_not_swallow_exception(name, make):
    tc.assert_exit_does_not_swallow_exception(make)


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_record_tool_visible_in_events(name, make):
    tc.assert_record_tool_visible_in_events(make)


@pytest.mark.parametrize("name,make", TRACERS, ids=[n for n, _ in TRACERS])
def test_success_status_after_clean_finish(name, make):
    tc.assert_success_status_after_clean_finish(make)
