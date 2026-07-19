"""Trajectory assertion engine (README §14.3, §11.3 ``trace:`` form).

A ``trace:`` assertion is a self-contained check over a normalised
``TraceEvent[]`` sequence — it never calls a model, so it is deterministic and
cheap and forms the ``trace_conformance`` slot of the skill-behaviour /
robustness gate families (README §16.1).

**Pattern grammar** (normative, README §14.3). A pattern is ``<verb>`` or
``<verb>:<target-glob>``:

* ``<verb>`` matches a ``tool_call`` event whose normalised name equals the
  verb exactly (case-sensitive).
* ``<target-glob>`` is a case-sensitive glob (``fnmatch``) matched against the
  event's ``target`` (file path, URL, or command string).

Built-in checks (extensible via ``register_check``):

===========================  ==================================================
``allow_tools(a,b,...)``     every ``tool_call`` matches one listed pattern
``deny_tools(a,b,...)``      no ``tool_call`` matches any listed pattern
``require_order(x, y)``      every ``y`` match is preceded by some ``x`` match
``max_calls(tool, n)``       at most ``n`` ``tool_call`` matches for ``tool``
``no_repeat_loops(n)``       no identical call repeated ``n``+ times in a row
``used_skill(id)``           a ``skill_activated`` lifecycle event with target
                             ``id`` (defaults to the case's ``expected_skill``)
===========================  ==================================================

Imports: ``core.interfaces`` + stdlib only.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from wololo_evals.core.interfaces import ScoredSignal, TraceEvent

__all__ = [
    "TraceAssertionError",
    "TraceCheckResult",
    "is_trace_assertion",
    "parse_trace_assertion",
    "evaluate_trace_assertion",
    "register_check",
    "TRACE_CONFORMANCE_SLOT",
]

#: §16.1 slot every ``trace:`` assertion feeds.
TRACE_CONFORMANCE_SLOT = "trace_conformance"

_ASSERTION_RE = re.compile(r"^trace:(?P<check>[A-Za-z_]\w*)\((?P<args>.*)\)$", re.DOTALL)


class TraceAssertionError(ValueError):
    """Raised when a ``trace:`` assertion is syntactically malformed or names
    an unknown check — fail loudly rather than silently pass an unparseable
    conformance requirement."""


@dataclass(frozen=True)
class TraceCheckResult:
    """Outcome of one ``trace:`` assertion. ``passed`` gates the
    ``trace_conformance`` slot; ``evidence`` is a human-readable reason;
    ``details`` carries structured specifics for ``analyse``."""

    check: str
    passed: bool
    evidence: str
    details: dict

    def to_signal(self, *, evaluator_id: str, weight: float = 1.0) -> ScoredSignal:
        """Adapt to the scoring pillar's ``ScoredSignal`` (kind ``assertion``,
        feeding the ``trace_conformance`` slot, README §15.3 / §16.1)."""
        return ScoredSignal(
            evaluator_id=evaluator_id,
            kind="assertion",
            score=1.0 if self.passed else 0.0,
            weight=weight,
            verdict="pass" if self.passed else "fail",
            slot=TRACE_CONFORMANCE_SLOT,
            details=self.details,
            evidence=self.evidence,
        )


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------
def _split_pattern(pattern: str) -> tuple[str, Optional[str]]:
    pattern = pattern.strip()
    if ":" in pattern:
        verb, glob = pattern.split(":", 1)
        return verb.strip(), glob.strip()
    return pattern, None


def _matches(event: TraceEvent, pattern: str) -> bool:
    """True iff ``event`` is a ``tool_call`` matching the pattern grammar."""
    if event.kind != "tool_call":
        return False
    verb, glob = _split_pattern(pattern)
    if event.name != verb:
        return False
    if glob is None:
        return True
    return fnmatch.fnmatchcase(event.target or "", glob)


def _tool_calls(events: Sequence[TraceEvent]) -> list[TraceEvent]:
    return [e for e in events if e.kind == "tool_call"]


# ---------------------------------------------------------------------------
# Built-in checks. Each: (args, events, default_skill) -> TraceCheckResult
# ---------------------------------------------------------------------------
def _check_allow_tools(args, events, default_skill) -> TraceCheckResult:
    allowed = [a for a in args if a]
    offenders = [
        e for e in _tool_calls(events)
        if not any(_matches(e, p) for p in allowed)
    ]
    passed = not offenders
    names = sorted({f"{e.name}:{e.target}" if e.target else e.name for e in offenders})
    return TraceCheckResult(
        check="allow_tools",
        passed=passed,
        evidence="all tool calls allowed" if passed
        else f"disallowed tool calls: {names}",
        details={"allowed": allowed, "offenders": names},
    )


def _check_deny_tools(args, events, default_skill) -> TraceCheckResult:
    denied = [a for a in args if a]
    offenders = [
        e for e in _tool_calls(events)
        if any(_matches(e, p) for p in denied)
    ]
    passed = not offenders
    names = sorted({f"{e.name}:{e.target}" if e.target else e.name for e in offenders})
    return TraceCheckResult(
        check="deny_tools",
        passed=passed,
        evidence="no denied tool called" if passed
        else f"denied tool calls: {names}",
        details={"denied": denied, "offenders": names},
    )


def _check_require_order(args, events, default_skill) -> TraceCheckResult:
    if len(args) != 2:
        raise TraceAssertionError(
            f"require_order takes 2 patterns, got {len(args)}: {args}"
        )
    before, after = args[0], args[1]
    calls = _tool_calls(events)
    seen_before = False
    violations = 0
    for e in calls:
        if _matches(e, before):
            seen_before = True
        if _matches(e, after) and not seen_before:
            violations += 1
    passed = violations == 0
    return TraceCheckResult(
        check="require_order",
        passed=passed,
        evidence=(
            f"every {after!r} preceded by {before!r}" if passed
            else f"{violations} {after!r} event(s) not preceded by {before!r}"
        ),
        details={"before": before, "after": after, "violations": violations},
    )


def _check_max_calls(args, events, default_skill) -> TraceCheckResult:
    if len(args) != 2:
        raise TraceAssertionError(
            f"max_calls takes (tool, n), got {args}"
        )
    tool = args[0]
    try:
        limit = int(args[1])
    except ValueError:
        raise TraceAssertionError(f"max_calls n must be an int, got {args[1]!r}") from None
    count = sum(1 for e in _tool_calls(events) if _matches(e, tool))
    passed = count <= limit
    return TraceCheckResult(
        check="max_calls",
        passed=passed,
        evidence=f"{tool!r} called {count} time(s), limit {limit}",
        details={"tool": tool, "count": count, "limit": limit},
    )


def _check_no_repeat_loops(args, events, default_skill) -> TraceCheckResult:
    if len(args) != 1:
        raise TraceAssertionError(f"no_repeat_loops takes (n), got {args}")
    try:
        limit = int(args[0])
    except ValueError:
        raise TraceAssertionError(
            f"no_repeat_loops n must be an int, got {args[0]!r}"
        ) from None
    if limit < 2:
        raise TraceAssertionError("no_repeat_loops n must be >= 2")
    calls = _tool_calls(events)
    worst = 1
    run = 1
    worst_call = None
    for i in range(1, len(calls)):
        prev, cur = calls[i - 1], calls[i]
        if (prev.name, prev.target) == (cur.name, cur.target):
            run += 1
            if run > worst:
                worst, worst_call = run, (cur.name, cur.target)
        else:
            run = 1
    passed = worst < limit
    return TraceCheckResult(
        check="no_repeat_loops",
        passed=passed,
        evidence=(
            f"longest identical run {worst}, limit {limit}" if passed
            else f"call {worst_call} repeated {worst} times consecutively (limit {limit})"
        ),
        details={"longest_run": worst, "limit": limit, "call": worst_call},
    )


def _check_used_skill(args, events, default_skill) -> TraceCheckResult:
    skill_id = (args[0].strip() if args and args[0].strip() else None) or default_skill
    if not skill_id:
        raise TraceAssertionError(
            "used_skill needs a skill id (explicit arg or case expected_skill)"
        )
    activations = [
        e.target for e in events
        if e.is_skill_activation
    ]
    passed = skill_id in activations
    return TraceCheckResult(
        check="used_skill",
        passed=passed,
        evidence=(
            f"skill {skill_id!r} activated" if passed
            else f"skill {skill_id!r} never activated (saw: {sorted(set(a for a in activations if a))})"
        ),
        details={"skill": skill_id, "activations": activations},
    )


_CheckFn = Callable[[list[str], Sequence[TraceEvent], Optional[str]], TraceCheckResult]

_CHECKS: dict[str, _CheckFn] = {
    "allow_tools": _check_allow_tools,
    "deny_tools": _check_deny_tools,
    "require_order": _check_require_order,
    "max_calls": _check_max_calls,
    "no_repeat_loops": _check_no_repeat_loops,
    "used_skill": _check_used_skill,
}


def register_check(name: str, fn: _CheckFn) -> None:
    """Register an extra ``trace:`` check (README §14.3: the registry is
    extensible). ``fn`` takes ``(args, events, default_skill)``."""
    _CHECKS[name] = fn


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------
def is_trace_assertion(text: str) -> bool:
    """True iff ``text`` is a ``trace:`` assertion (README §11.3)."""
    return text.strip().startswith("trace:")


def parse_trace_assertion(text: str) -> tuple[str, list[str]]:
    """Parse ``trace:check(a, b, ...)`` → ``("check", ["a", "b", ...])``.

    Splits args on top-level commas; an empty arg list is ``[]``. Raises
    ``TraceAssertionError`` on malformed syntax.
    """
    m = _ASSERTION_RE.match(text.strip())
    if not m:
        raise TraceAssertionError(f"malformed trace assertion: {text!r}")
    check = m.group("check")
    raw_args = m.group("args").strip()
    args = [a.strip() for a in raw_args.split(",")] if raw_args else []
    return check, args


def evaluate_trace_assertion(
    text: str,
    events: Sequence[TraceEvent],
    *,
    default_skill: Optional[str] = None,
) -> TraceCheckResult:
    """Evaluate one ``trace:`` assertion against a normalised trace.

    ``default_skill`` supplies the ``used_skill`` id when the assertion omits
    it (README §14.3: defaults to the case's ``expected_skill``). Unknown
    checks and malformed syntax raise ``TraceAssertionError``.
    """
    check, args = parse_trace_assertion(text)
    fn = _CHECKS.get(check)
    if fn is None:
        raise TraceAssertionError(
            f"unknown trace check {check!r} (have: {sorted(_CHECKS)})"
        )
    return fn(args, events, default_skill)
