"""Invariant 3, validation level: OTEL spans + per-tenant metering are queried
back out of an in-memory exporter.

The full rung stands the OTLP debug collector up in docker compose and reads
spans back over the wire (OPERATOR precondition: no docker daemon here). This
test proves the same shape against an in-process exporter: a span carries
``tenant_id`` and a ``wololo.metering`` event carries the metered unit/amount
keyed by tenant. If ``record_metering`` were called outside a recording span,
the event would be dropped and this test would fail.
"""
from __future__ import annotations

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from wololo_evals.otel import record_metering


def _provider_with_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "wololo-evals"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_route_span_and_metering_read_back_out():
    provider, exporter = _provider_with_exporter()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("POST /api/v1/candidates") as span:
        span.set_attribute("tenant_id", "meridian")
        record_metering("meridian", "tokens", 1234, **{"metering.arm": "treatment"})
        record_metering("meridian", "dollars", 0.021)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["tenant_id"] == "meridian"

    metering = [e for e in span.events if e.name == "wololo.metering"]
    assert len(metering) == 2
    units = {e.attributes["metering.unit"]: e.attributes for e in metering}
    assert units["tokens"]["metering.amount"] == 1234
    assert units["tokens"]["tenant_id"] == "meridian"
    assert units["tokens"]["metering.arm"] == "treatment"
    assert units["dollars"]["metering.amount"] == 0.021


def test_metering_outside_span_is_dropped_not_crash():
    """No current recording span => the event lands on a non-recording span
    and is silently dropped (never a crash). The invariant test above is what
    guarantees the in-route path actually records."""
    record_metering("meridian", "tokens", 1)  # must not raise
