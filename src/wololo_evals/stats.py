"""Statistics engine (SPEC S7, DOCTRINE 15.6: no verdict on n=1).

Uniform policy across suites: verdict-bearing runs execute ``n_trials`` per
arm (floor 5, DDL-enforced); trials pair by ``(task_fixture, trial_index)``;
the run's verdict stores mean, stddev, a 95% Student-t confidence interval
over the paired per-trial differences, wins/losses, and ``low_confidence``
(CI spanning zero). stdlib only: ``statistics`` + ``math`` + a small t-table,
no scipy for a t-quantile of tiny n.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence

# Two-sided 95% Student-t critical values by degrees of freedom.
_T_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
    14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
    20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t_critical_95(df: int) -> float:
    if df < 1:
        raise ValueError("degrees of freedom must be >= 1")
    # ponytail: beyond the table the normal quantile is within 2% of t.
    return _T_95.get(df, 1.960)


def paired_uplift_pcts(
    baseline: Sequence[float], treatment: Sequence[float]
) -> list[float]:
    """Per-pair percentage uplift of treatment over baseline.

    Pairs are index-aligned (trial i baseline vs trial i treatment, identical
    task/checkout/image). A non-positive baseline composite cannot anchor a
    ratio; that pair contributes absolute percentage points instead
    (documented, deterministic, rare: composites are 0..1 weighted sums).
    """
    if len(baseline) != len(treatment):
        raise ValueError(
            f"unpaired trials: {len(baseline)} baseline vs {len(treatment)} treatment"
        )
    if not baseline:
        raise ValueError("no completed trial pairs")
    diffs = []
    for b, t in zip(baseline, treatment):
        if b > 0:
            diffs.append((t - b) / b * 100.0)
        else:
            diffs.append((t - b) * 100.0)
    return diffs


def verdict_stats(diffs: Sequence[float]) -> dict:
    """The ``verdict_stats`` blob from paired per-trial differences."""
    n = len(diffs)
    if n < 1:
        raise ValueError("verdict requires at least one paired difference")
    mean = statistics.fmean(diffs)
    stddev = statistics.stdev(diffs) if n > 1 else 0.0
    if n > 1:
        half = t_critical_95(n - 1) * stddev / math.sqrt(n)
        ci_low, ci_high = mean - half, mean + half
    else:
        # n=1 is never verdict-bearing; the interval is degenerate and the
        # low_confidence flag below forces it visible.
        ci_low = ci_high = mean
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d < 0)
    low_confidence = (ci_low <= 0.0 <= ci_high) or n < 2
    return {
        "mean": mean,
        "stddev": stddev,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "wins": wins,
        "losses": losses,
        "n": n,
        "low_confidence": low_confidence,
    }


def task_uplift_passed(stats: dict, min_effect_pct: float) -> bool:
    """SPEC S7 pass rule: wins >= ceil(0.6*n) AND mean uplift >= min effect.

    ``low_confidence`` does not flip a pass to fail here — it blocks
    canary-gate opening (409) and stays visible on the leaderboard.
    """
    return (
        stats["wins"] >= math.ceil(0.6 * stats["n"])
        and stats["mean"] >= min_effect_pct
    )


def single_arm_stats(samples: Sequence[float], pass_bar: Optional[float] = None) -> dict:
    """verdict_stats shape for single-arm suites (skill-review, replay).

    The "difference" per judge sample is the margin over the pass bar when a
    bar is given (so a knife-edge pass — composite within one stddev of the
    bar — reads directly off ``low_confidence``), else the raw samples.
    """
    if not samples:
        raise ValueError("no samples")
    values = [s - pass_bar for s in samples] if pass_bar is not None else list(samples)
    return verdict_stats(values)
