"""Standing trajectory efficiency signals (README §14.3).

The trajectory pillar emits an efficiency signal for *every* run — regardless
of any authored ``trace:`` assertion — so leaderboards and ``analyse
--efficiency`` can compare how *economically* two combos reach the same
outcome. These signals land in ``signals[]`` with ``kind == "efficiency"`` and
**do not gate by default** (weight 0), so a chatty-but-correct run is visible
without being failed.

Signals produced:

* ``efficiency.total_tool_calls`` — number of ``tool_call`` events.
* ``efficiency.wasted_calls`` — read/fetch/search calls whose ``target`` is
  never subsequently referenced by an edit/write/bash call (result unused).
* ``efficiency.tokens_in`` / ``efficiency.tokens_out`` — token cost summed off
  the trace ("tokens per solved case" when the caller only emits these for
  solved cases).
* ``efficiency.wall_clock_ms`` — span of the trace clock (``ts_ms``), the
  trajectory's wall-clock proxy.

Imports: ``core.interfaces`` + stdlib only.
"""
from __future__ import annotations

from typing import Sequence

from wololo_evals.core.interfaces import ScoredSignal, TraceEvent

__all__ = [
    "total_tool_calls",
    "wasted_calls",
    "token_totals",
    "wall_clock_ms",
    "efficiency_signals",
]

#: verbs that *produce* information (a read whose result may go unused).
_PRODUCER_VERBS = frozenset({"read", "fetch", "search"})
#: verbs that *consume* a prior read (evidence the read was not wasted).
_CONSUMER_VERBS = frozenset({"edit", "write", "bash"})


def total_tool_calls(events: Sequence[TraceEvent]) -> int:
    """Count ``tool_call`` events (lifecycle activations excluded)."""
    return sum(1 for e in events if e.kind == "tool_call")


def wasted_calls(events: Sequence[TraceEvent]) -> int:
    """Read/fetch/search calls whose ``target`` is never referenced by a later
    edit/write/bash call.

    Definition (README §14.3 "wasted calls (result unused)"): a producer call
    is wasted iff no later consumer call's ``target`` equals or contains the
    producer's ``target``. A producer with no ``target`` is never counted
    wasted (nothing to correlate).
    """
    calls = [e for e in events if e.kind == "tool_call"]
    wasted = 0
    for i, prod in enumerate(calls):
        if prod.name not in _PRODUCER_VERBS or not prod.target:
            continue
        used = False
        for cons in calls[i + 1:]:
            if cons.name not in _CONSUMER_VERBS or not cons.target:
                continue
            if prod.target == cons.target or prod.target in cons.target:
                used = True
                break
        if not used:
            wasted += 1
    return wasted


def token_totals(events: Sequence[TraceEvent]) -> tuple[int, int]:
    """``(tokens_in, tokens_out)`` summed across the trace."""
    return (
        sum(e.tokens_in for e in events),
        sum(e.tokens_out for e in events),
    )


def wall_clock_ms(events: Sequence[TraceEvent]) -> int:
    """Span of the trace clock (last ``ts_ms`` minus first); 0 for <2 events."""
    if len(events) < 2:
        return 0
    stamps = [e.ts_ms for e in events]
    return max(stamps) - min(stamps)


def _signal(evaluator_id: str, value: float) -> ScoredSignal:
    """A non-gating efficiency signal: verdict ``pass``, weight 0, metric in
    ``details`` (README §14.3: efficiency signals do not gate)."""
    return ScoredSignal(
        evaluator_id=evaluator_id,
        kind="efficiency",
        score=1.0,
        weight=0.0,
        verdict="pass",
        slot=None,
        details={"value": value},
        evidence=f"{evaluator_id}={value}",
    )


def efficiency_signals(events: Sequence[TraceEvent]) -> list[ScoredSignal]:
    """The full standing efficiency signal set for one run's trace."""
    tin, tout = token_totals(events)
    return [
        _signal("efficiency.total_tool_calls", total_tool_calls(events)),
        _signal("efficiency.wasted_calls", wasted_calls(events)),
        _signal("efficiency.tokens_in", tin),
        _signal("efficiency.tokens_out", tout),
        _signal("efficiency.wall_clock_ms", wall_clock_ms(events)),
    ]
