"""Shared ISO-8601 timestamp boundary check for contract builders.

The JSON schemas carry ``format: date-time`` for documentation, but JSON
Schema validators do not assert ``format`` by default (jsonschema needs an
opt-in FormatChecker; AJV needs ajv-formats), so every ``ts``-bearing schema
ALSO carries this pattern — a constraint that is real in every validator.
Builders reject a malformed timestamp here so an invalid payload cannot be
constructed in the first place.

Keep :data:`ISO8601_PATTERN` in lockstep with the ``pattern`` on ``ts`` /
``timestamp`` / ``first_seen`` in ``specs/agentic-engineering/schemas/
{event,learning-record}.schema.json`` and
``specs/mission-control/schemas/event.schema.json`` —
``tests/test_contract_schemas.py::test_ts_pattern_in_schemas_matches_builders``
enforces the agreement.
"""
from __future__ import annotations

import re
from datetime import datetime

__all__ = ["ISO8601_PATTERN", "ISO8601_RE", "check_iso8601"]

# Accepts what the fleet emits (time.strftime('%Y-%m-%dT%H:%M:%SZ')) plus
# fractional seconds and numeric UTC offsets from third-party producers.
ISO8601_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
ISO8601_RE = re.compile(ISO8601_PATTERN)


def check_iso8601(value: str, field: str = "ts") -> str:
    """Return *value* unchanged, or raise ``ValueError`` when it is not an
    ISO-8601 date-time (e.g. ``2026-07-04T00:00:00Z``)."""
    if not ISO8601_RE.match(value or ""):
        raise ValueError(
            f"{field} {value!r} is not an ISO-8601 date-time "
            "(expected e.g. 2026-07-04T00:00:00Z)"
        )
    # The regex (mirrored into the JSON Schema ``pattern``) only checks shape;
    # it lets calendar-impossible values through (month 13, day 99). Parse to
    # reject those so no builder can mint an audit/event row with a timestamp
    # that no clock could produce. ponytail: fromisoformat covers calendar
    # validity; drop this if the pattern ever gains full date semantics.
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field} {value!r} is ISO-8601-shaped but not a real calendar "
            "date-time"
        ) from exc
    return value
