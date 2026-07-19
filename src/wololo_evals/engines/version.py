"""Engine-CLI version pinning (README §6.1).

Each adapter captures its CLI's ``--version`` and refuses to run when the
installed version falls outside ``engines.<id>.version_range`` from the matrix
config — unless ``--allow-engine-drift`` is set. ``replay`` is exempt (§14.4)
and never calls this module.

The range grammar is a comma-separated conjunction of comparator clauses over
``major.minor.patch`` semver cores (pre-release/build metadata ignored):

    ">=1.0.0,<3.0.0"      installed must satisfy both
    "==1.2.3"             exact
    "*"  /  ""            any version (no pinning)

Supported comparators: ``>=`` ``<=`` ``>`` ``<`` ``==`` ``!=`` ``~=``
(``~=1.2`` is PEP 440 compatible-release: ``>=1.2, <2``; ``~=1.2.3``:
``>=1.2.3, <1.3``). Stdlib only — no ``semver``/``packaging`` dependency, so
the open ``engines/`` dir stays import-light.
"""
from __future__ import annotations

import re
from typing import Tuple

__all__ = ["EngineVersionError", "parse_version", "satisfies", "check_range"]

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_CLAUSE_RE = re.compile(r"^\s*(>=|<=|==|!=|~=|>|<)?\s*(.+?)\s*$")

Version = Tuple[int, int, int]


class EngineVersionError(RuntimeError):
    """Installed CLI version is outside the pinned range and drift is not
    allowed. Refusing to run keeps a silently-upgraded CLI from poisoning a
    verdict with output the parsers were never validated against."""


def parse_version(raw: str) -> Version:
    """Extract the first ``major.minor.patch`` core from a ``--version`` string
    (e.g. ``"claude 1.4.2 (Claude Code)"`` -> ``(1, 4, 2)``). Missing minor or
    patch default to 0. Fails loudly if no numeric core is present rather than
    guessing ``(0, 0, 0)``."""
    m = _VERSION_RE.search(raw or "")
    if m is None:
        raise EngineVersionError(f"no version core found in {raw!r}")
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def _cmp(a: Version, b: Version) -> int:
    return (a > b) - (a < b)


def _satisfies_clause(installed: Version, op: str, bound: Version, bound_raw: str) -> bool:
    if op == "~=":
        # compatible release: pin all but the last specified component
        parts = bound_raw.split(".")
        if len(parts) < 2:
            raise EngineVersionError(f"~= needs at least major.minor, got {bound_raw!r}")
        if len(parts) == 2:  # ~=1.2 -> >=1.2.0, <2.0.0
            upper: Version = (bound[0] + 1, 0, 0)
        else:  # ~=1.2.3 -> >=1.2.3, <1.3.0
            upper = (bound[0], bound[1] + 1, 0)
        return _cmp(installed, bound) >= 0 and _cmp(installed, upper) < 0
    c = _cmp(installed, bound)
    return {
        ">=": c >= 0,
        "<=": c <= 0,
        ">": c > 0,
        "<": c < 0,
        "==": c == 0,
        "!=": c != 0,
    }[op]


def satisfies(installed: str, version_range: str) -> bool:
    """True iff ``installed`` satisfies every clause of ``version_range``.
    ``"*"`` or an empty range matches anything."""
    rng = (version_range or "").strip()
    if rng in ("", "*"):
        return True
    iv = parse_version(installed)
    for clause in rng.split(","):
        m = _CLAUSE_RE.match(clause)
        if not m or not m.group(2):
            raise EngineVersionError(f"unparseable version clause {clause!r}")
        op = m.group(1) or "=="
        bound_raw = m.group(2)
        bound = parse_version(bound_raw)
        if not _satisfies_clause(iv, op, bound, bound_raw):
            return False
    return True


def check_range(installed: str, version_range: str, *, allow_drift: bool) -> None:
    """Raise :class:`EngineVersionError` if ``installed`` is outside
    ``version_range`` and ``allow_drift`` is false; otherwise return ``None``
    (a drift-allowed mismatch is a caller's problem to log, not to block)."""
    if satisfies(installed, version_range):
        return
    if allow_drift:
        return
    raise EngineVersionError(
        f"installed CLI version {installed!r} is outside pinned range "
        f"{version_range!r}; pass --allow-engine-drift to override"
    )
