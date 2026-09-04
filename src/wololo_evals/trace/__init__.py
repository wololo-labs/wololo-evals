"""Trajectory pillar (open, Apache-2.0): normalise engine-native event streams
into the ``TraceEvent`` schema, evaluate ``trace:`` assertions over them, and
emit standing efficiency signals (README §14.3).

Public surface:

* ``TraceEvent`` — re-exported from ``core.interfaces`` (the single schema
  every builder shares).
* ``get_normaliser`` / ``NORMALISERS`` / ``normalise_stream`` — per-engine
  raw-stream → ``TraceEvent[]``, failing loudly on unrecognised formats.
* ``evaluate_trace_assertion`` / ``TraceCheckResult`` / ``is_trace_assertion``
  / ``register_check`` — the ``trace:`` assertion engine.
* ``efficiency_signals`` (+ the individual metric helpers) — non-gating
  trajectory efficiency signals.
* ``write_trace`` / ``read_trace`` — canonical ``trace.jsonl`` io.

This package imports only ``core.interfaces`` and the standard library — no
database, no vendor SDK.
"""
from __future__ import annotations

from wololo_evals.core.interfaces import TraceEvent

from wololo_evals.trace.assertions import (
    TRACE_CONFORMANCE_SLOT,
    TraceAssertionError,
    TraceCheckResult,
    evaluate_trace_assertion,
    is_trace_assertion,
    parse_trace_assertion,
    register_check,
)
from wololo_evals.trace.efficiency import (
    efficiency_signals,
    token_totals,
    total_tool_calls,
    wall_clock_ms,
    wasted_calls,
)
from wololo_evals.trace.io import (
    TRACE_FIELD_ORDER,
    TRACE_SCHEMA_VERSION,
    dumps_event,
    read_trace,
    write_trace,
)
from wololo_evals.trace.normalisers import (
    NORMALISERS,
    ClaudeNormaliser,
    CodexNormaliser,
    CopilotNormaliser,
    ReplayNormaliser,
    TraceParseError,
    get_normaliser,
    normalise_stream,
)

__all__ = [
    "TraceEvent",
    # normalisers
    "NORMALISERS",
    "ClaudeNormaliser",
    "CodexNormaliser",
    "CopilotNormaliser",
    "ReplayNormaliser",
    "TraceParseError",
    "get_normaliser",
    "normalise_stream",
    # assertions
    "TRACE_CONFORMANCE_SLOT",
    "TraceAssertionError",
    "TraceCheckResult",
    "evaluate_trace_assertion",
    "is_trace_assertion",
    "parse_trace_assertion",
    "register_check",
    # efficiency
    "efficiency_signals",
    "token_totals",
    "total_tool_calls",
    "wall_clock_ms",
    "wasted_calls",
    # io
    "TRACE_FIELD_ORDER",
    "TRACE_SCHEMA_VERSION",
    "dumps_event",
    "read_trace",
    "write_trace",
]
