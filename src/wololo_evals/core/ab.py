"""A/B verdict math for the ``gate``/``analyse`` presenters (README §16.2–§16.4).

This is the closed-core statistical layer the open ``gate`` CLI reads to turn a
candidate-vs-baseline pair of matrix runs into a significance-gated verdict:

* **Paired-by-case bootstrap** (:func:`paired_bootstrap_ci`) — a percentile
  bootstrap (>= 1000 resamples by default) over the per-case score deltas,
  yielding the confidence interval on the *benchmark-score delta* (§16.2). The
  regression rule (§16.4) reads its **upper** bound against the tolerance-shifted
  null (``ci_high < -max_regression``).
* **Four-bucket classification** (:func:`classify_case`, :func:`bucket_counts`) —
  each case is ``working`` / ``fixed`` / ``regression`` / ``still_gap`` from the
  (baseline_pass, candidate_pass) pair (§16.3).
* **Wilson score interval** (:func:`wilson_interval`) — the interval on a pass
  rate the leaderboard/gate reports (§16.2).
* **Verdict assembly** (:func:`compare`) — folds the above plus the
  newly-failing-hard-gate rule into a single :class:`ABVerdict`.

Pure stdlib (``statistics``/``math``/``random``); wraps ``core.stats`` where a
wave-1 primitive already fits. Deterministic: the bootstrap draws from a
seeded :class:`random.Random`, so a re-run over identical deltas is byte-stable
(the same discipline ``replay`` relies on elsewhere). Imports no database, no
engine, no network — the closed-core hard rule.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

__all__ = [
    "BUCKETS",
    "classify_case",
    "CasePair",
    "bucket_counts",
    "paired_bootstrap_ci",
    "wilson_interval",
    "ABVerdict",
    "compare",
]

#: The four case-comparison buckets (§16.3), in report order.
BUCKETS = ("working", "fixed", "regression", "still_gap")


def _z_critical(confidence: float) -> float:
    """Two-sided normal critical value for ``confidence`` (e.g. 0.95 -> ~1.96)."""
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    return statistics.NormalDist().inv_cdf((1.0 + confidence) / 2.0)


def classify_case(baseline_passed: bool, candidate_passed: bool) -> str:
    """Map a (baseline, candidate) pass pair onto its §16.3 bucket.

    ``working`` both pass, ``fixed`` candidate passes where baseline failed,
    ``regression`` candidate fails where baseline passed, ``still_gap`` both
    fail.
    """
    if baseline_passed and candidate_passed:
        return "working"
    if not baseline_passed and candidate_passed:
        return "fixed"
    if baseline_passed and not candidate_passed:
        return "regression"
    return "still_gap"


@dataclass(frozen=True)
class CasePair:
    """One case scored under both runs, paired by ``case_id``.

    ``*_hard_failed`` records whether that run's case tripped a hard gate
    (§16.1 step 2) — a *newly* failing hard gate on the candidate side fails the
    gate regardless of statistical significance (§16.4). ``quarantined`` cases
    are excluded from every gate-facing aggregate but stay in the report.
    """

    case_id: str
    baseline_score: float
    candidate_score: float
    baseline_passed: bool
    candidate_passed: bool
    baseline_hard_failed: bool = False
    candidate_hard_failed: bool = False
    quarantined: bool = False
    case_weight: float = 1.0

    @property
    def delta(self) -> float:
        return self.candidate_score - self.baseline_score

    @property
    def bucket(self) -> str:
        return classify_case(self.baseline_passed, self.candidate_passed)

    @property
    def newly_failing_hard_gate(self) -> bool:
        return self.candidate_hard_failed and not self.baseline_hard_failed


def bucket_counts(pairs: Sequence[CasePair]) -> dict[str, int]:
    """Count each §16.3 bucket over ``pairs`` (all four keys always present)."""
    counts = {b: 0 for b in BUCKETS}
    for p in pairs:
        counts[p.bucket] += 1
    return counts


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    wsum = math.fsum(weights)
    if wsum <= 0:
        return 0.0
    return math.fsum(v * w for v, w in zip(values, weights)) / wsum


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile (0..1) of an already-sorted sequence."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def paired_bootstrap_ci(
    deltas: Sequence[float],
    *,
    resamples: int = 1000,
    confidence: float = 0.95,
    weights: Optional[Sequence[float]] = None,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile-bootstrap CI on the mean of paired-by-case score deltas (§16.2).

    ``deltas[i]`` is ``candidate_score - baseline_score`` for case ``i`` (already
    paired). Each of ``resamples`` (>= 1000, per §16.2) draws ``len(deltas)``
    cases with replacement and records the (optionally case-weight-weighted)
    mean delta; the returned interval is the ``confidence`` percentile band over
    those resample means — i.e. the CI on the benchmark-score delta. The draw is
    seeded so the interval is deterministic for identical inputs.
    """
    n = len(deltas)
    if n == 0:
        raise ValueError("paired_bootstrap_ci requires at least one delta")
    if resamples < 1000:
        raise ValueError("resamples must be >= 1000 (§16.2)")
    if weights is None:
        weights = [1.0] * n
    elif len(weights) != n:
        raise ValueError(f"weights length {len(weights)} != deltas length {n}")

    rng = random.Random(seed)
    idxs = range(n)
    means: list[float] = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in idxs]
        means.append(
            _weighted_mean([deltas[i] for i in picks], [weights[i] for i in picks])
        )
    means.sort()
    alpha = 1.0 - confidence
    lo = _percentile(means, alpha / 2.0)
    hi = _percentile(means, 1.0 - alpha / 2.0)
    return lo, hi


def wilson_interval(
    passes: int, n: int, *, confidence: float = 0.95
) -> tuple[float, float]:
    """Wilson score interval on a pass rate (§16.2).

    ``passes`` of ``n`` seeds/cases passed. Returns the ``confidence`` interval
    on the underlying pass probability, clamped to ``[0, 1]``. ``n == 0`` yields
    the vacuous ``(0.0, 1.0)`` — no observations constrain the rate.
    """
    if passes < 0 or n < 0 or passes > n:
        raise ValueError(f"invalid pass count {passes}/{n}")
    if n == 0:
        return 0.0, 1.0
    z = _z_critical(confidence)
    phat = passes / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2.0 * n)) / denom
    half = (z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))) / denom
    return max(0.0, center - half), min(1.0, center + half)


@dataclass(frozen=True)
class ABVerdict:
    """Outcome of comparing a candidate run against a baseline (§16.4).

    ``regressed`` is the gate's fail signal: it is true when the delta CI upper
    bound falls below ``-max_regression`` (a statistically significant
    regression against the tolerance-shifted null) **or** any case newly trips a
    hard gate. ``significant_regression`` isolates just the statistical arm;
    ``newly_failing_hard_gates`` lists the case ids of the other arm.
    """

    mean_delta: float
    ci_low: float
    ci_high: float
    confidence: float
    max_regression: float
    significant_regression: bool
    significant_improvement: bool
    newly_failing_hard_gates: tuple[str, ...]
    buckets: dict[str, int]
    n_cases: int
    n_quarantined: int

    @property
    def regressed(self) -> bool:
        return self.significant_regression or bool(self.newly_failing_hard_gates)


def compare(
    pairs: Sequence[CasePair],
    *,
    max_regression: float = 0.0,
    resamples: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
) -> ABVerdict:
    """Assemble the §16.4 verdict from paired cases.

    Quarantined cases are dropped from the delta bootstrap, the bucket counts,
    and the hard-gate scan (they stay visible in the caller's report). The
    regression rule reads the CI **upper** bound against ``-max_regression``.
    """
    gating = [p for p in pairs if not p.quarantined]
    n_quarantined = len(pairs) - len(gating)

    if gating:
        deltas = [p.delta for p in gating]
        weights = [p.case_weight for p in gating]
        mean_delta = _weighted_mean(deltas, weights)
        ci_low, ci_high = paired_bootstrap_ci(
            deltas,
            resamples=resamples,
            confidence=confidence,
            weights=weights,
            seed=seed,
        )
    else:
        mean_delta = ci_low = ci_high = 0.0

    significant_regression = bool(gating) and ci_high < -max_regression
    significant_improvement = bool(gating) and ci_low > max_regression
    hard = tuple(p.case_id for p in gating if p.newly_failing_hard_gate)

    return ABVerdict(
        mean_delta=mean_delta,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence=confidence,
        max_regression=max_regression,
        significant_regression=significant_regression,
        significant_improvement=significant_improvement,
        newly_failing_hard_gates=hard,
        buckets=bucket_counts(gating),
        n_cases=len(gating),
        n_quarantined=n_quarantined,
    )
