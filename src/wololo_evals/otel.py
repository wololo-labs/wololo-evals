"""OTEL traces + per-tenant metering (invariant 3).

Every API route and every trial runs inside a span carrying ``tenant_id`` and
resource attributes; judge and harness spans use gen-ai semantic-convention
attribute names. Metering is emitted as ``wololo.metering`` span events
(unit, amount, tenant_id) so the collector-side FinOps pipeline reads cost
per tenant off the same stream as the traces.

Exporter wiring is injectable: tests install an in-memory exporter and query
spans back out; production points OTLP at the collector via the standard
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

_SERVICE = "wololo-evals"


def init_tracing(exporter: SpanExporter | None = None) -> TracerProvider:
    """Install a TracerProvider. With no exporter given, tries OTLP when the
    standard endpoint env var is set, else spans stay in-process (no-op
    export) — never a crash on a missing collector."""
    provider = TracerProvider(resource=Resource.create({"service.name": _SERVICE}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        import os

        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(_SERVICE)


def record_metering(tenant_id: str, unit: str, amount: float, **attributes) -> None:
    """Per-tenant metering event on the current span (tokens, minutes,
    dollars, rejected submissions). No current span = programming error made
    visible: the event lands on a non-recording span and is dropped, which
    the invariant-3 test would catch."""
    span = trace.get_current_span()
    span.add_event(
        "wololo.metering",
        attributes={
            "tenant_id": tenant_id,
            "metering.unit": unit,
            "metering.amount": amount,
            **attributes,
        },
    )
