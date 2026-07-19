"""Core stats facade: the wave-1 paired-trial engine plus the in-library
N>=5 trial-floor gate (SPEC S6.2 / S7, DOCTRINE 15.6: no verdict on n<floor).

``stats.py`` itself is deliberately floor-agnostic — ``verdict_stats`` /
``single_arm_stats`` happily compute a (degenerate, ``low_confidence``) CI for
any ``n >= 1``, and the frozen ``StatsEngine`` contract documents the floor as
*caller discipline*. In the wave-1 service that discipline is reconstructed in
three places (the ``tenants.n_trials`` DDL check, ``api.py``'s
``N_TRIALS_FLOOR`` + 422, and ``worker.py``'s short-arm→errored guards). A
DB-free caller (the file-first CLI, a bench, a gate) has none of that Postgres
scaffolding, so the floor would silently vanish.

This facade closes that gap: it re-exports the raw stats surface unchanged
(so ``core`` consumers get identical numbers) and adds an *explicit* floor
gate — :func:`check_trial_floor` / :func:`verdict_stats_floored` /
:func:`single_arm_stats_floored` — that raises ``ValueError`` below the floor.
It changes nothing about ``stats.py`` and nothing about the
``worker.py``/``api.py`` paths, which continue to import ``stats`` directly and
enforce the floor exactly as before.
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..stats import (
    paired_uplift_pcts,
    single_arm_stats,
    t_critical_95,
    task_uplift_passed,
    verdict_stats,
)

__all__ = [
    "N_TRIALS_FLOOR",
    "FloorViolation",
    "check_trial_floor",
    "verdict_stats_floored",
    "single_arm_stats_floored",
    # raw wave-1 surface, re-exported unchanged
    "verdict_stats",
    "single_arm_stats",
    "paired_uplift_pcts",
    "task_uplift_passed",
    "t_critical_95",
]

#: Minimum paired trials per arm for a verdict-bearing run. Mirrors the DDL
#: default+check on ``tenants.n_trials`` and ``api.py``'s ``N_TRIALS_FLOOR``;
#: this is the DB-free enforcement point that keeps them honest.
N_TRIALS_FLOOR = 5


class FloorViolation(ValueError):
    """A verdict was requested on fewer than :data:`N_TRIALS_FLOOR` trials.

    A ``ValueError`` subclass so existing ``except ValueError`` sites keep
    catching it while callers that care can distinguish a floor breach from a
    generic bad-input error.
    """


def check_trial_floor(n: int, *, smoke: bool = False) -> None:
    """Raise :class:`FloorViolation` when ``n`` is below the trial floor.

    ``smoke=True`` mirrors the wave-1 escape hatch (SPEC S6.2): smoke runs may
    execute fewer trials but are non-verdict-bearing and must never be consumed
    by the leaderboard or the gate. This function does not itself mark a run as
    smoke — it only declines to raise for one.
    """
    if smoke:
        return
    if not isinstance(n, int):
        raise TypeError(f"trial count must be an int, got {type(n).__name__}")
    if n < N_TRIALS_FLOOR:
        raise FloorViolation(
            f"verdict-bearing run needs at least {N_TRIALS_FLOOR} paired trials, "
            f"got {n}; pass smoke=True only for non-verdict smoke runs"
        )


def verdict_stats_floored(diffs: Sequence[float], *, smoke: bool = False) -> dict:
    """:func:`stats.verdict_stats` guarded by the in-library trial floor.

    Identical output to ``verdict_stats`` once the floor passes; raises
    :class:`FloorViolation` below the floor instead of returning a degenerate,
    silently ``low_confidence`` verdict.
    """
    check_trial_floor(len(diffs), smoke=smoke)
    return verdict_stats(diffs)


def single_arm_stats_floored(
    samples: Sequence[float],
    pass_bar: Optional[float] = None,
    *,
    smoke: bool = False,
) -> dict:
    """:func:`stats.single_arm_stats` guarded by the in-library trial floor."""
    check_trial_floor(len(samples), smoke=smoke)
    return single_arm_stats(samples, pass_bar)
