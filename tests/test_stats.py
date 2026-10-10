"""Golden math tests: known trial vectors in, known mean/CI/wins out (SPEC S7)."""
from __future__ import annotations

import math

import pytest

from wololo_evals.stats import (
    paired_uplift_pcts,
    single_arm_stats,
    t_critical_95,
    task_uplift_passed,
    verdict_stats,
)


def test_golden_paired_verdict():
    baseline = [0.50, 0.60, 0.55, 0.45, 0.50]
    treatment = [0.60, 0.66, 0.60, 0.54, 0.48]
    diffs = paired_uplift_pcts(baseline, treatment)
    # hand-computed: +20%, +10%, +9.0909..%, +20%, -4%
    assert diffs == pytest.approx([20.0, 10.0, 9.090909090909092, 20.0, -4.0])
    stats = verdict_stats(diffs)
    assert stats["n"] == 5
    assert stats["wins"] == 4
    assert stats["losses"] == 1
    assert stats["mean"] == pytest.approx(11.018181818181818)
    # sample stddev, n-1
    assert stats["stddev"] == pytest.approx(9.894985790876756)
    # 95% t CI, df=4, t=2.776: mean +/- 2.776 * s / sqrt(5)
    half = 2.776 * stats["stddev"] / math.sqrt(5)
    assert stats["ci_low"] == pytest.approx(stats["mean"] - half)
    assert stats["ci_high"] == pytest.approx(stats["mean"] + half)
    assert stats["low_confidence"] is True  # ci_low < 0 here: spans zero
    # pass rule: 4/5 wins >= ceil(0.6*5)=3 and mean 11.0 >= 5.0
    assert task_uplift_passed(stats, min_effect_pct=5.0)


def test_golden_tight_uplift_not_low_confidence():
    diffs = [10.0, 12.0, 11.0, 9.0, 13.0]
    stats = verdict_stats(diffs)
    assert stats["wins"] == 5
    assert stats["mean"] == pytest.approx(11.0)
    assert stats["ci_low"] > 0
    assert stats["low_confidence"] is False
    assert task_uplift_passed(stats, 5.0)


def test_pass_rule_requires_both_conditions():
    # all wins but tiny effect: fails min_effect_pct
    stats = verdict_stats([1.0, 1.5, 2.0, 1.2, 1.8])
    assert not task_uplift_passed(stats, 5.0)
    # big mean but only 2/5 wins: fails the wins rule
    stats = verdict_stats([80.0, 60.0, -5.0, -4.0, -3.0])
    assert stats["wins"] == 2
    assert not task_uplift_passed(stats, 5.0)


def test_zero_baseline_uses_absolute_points():
    diffs = paired_uplift_pcts([0.0], [0.5])
    assert diffs == [50.0]


def test_unpaired_or_empty_rejected():
    with pytest.raises(ValueError):
        paired_uplift_pcts([1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        paired_uplift_pcts([], [])
    with pytest.raises(ValueError):
        verdict_stats([])


def test_n1_is_flagged_low_confidence():
    stats = verdict_stats([42.0])
    assert stats["n"] == 1
    assert stats["low_confidence"] is True


def test_t_table():
    assert t_critical_95(4) == 2.776
    assert t_critical_95(100) >= 1.984  # the true t; the table steps down to the anchor at 60
    with pytest.raises(ValueError):
        t_critical_95(0)


def test_single_arm_knife_edge_visible():
    # composites hugging the pass bar: CI over margins spans zero
    stats = single_arm_stats([0.71, 0.69, 0.72, 0.68, 0.70], pass_bar=0.70)
    assert stats["low_confidence"] is True
    # comfortably above the bar: confident
    stats = single_arm_stats([0.85, 0.86, 0.84, 0.85, 0.87], pass_bar=0.70)
    assert stats["low_confidence"] is False


def test_t_critical_never_understates_beyond_the_table():
    # Beyond df 30 the function returned the normal 1.960, but t(31) is 2.040,
    # so the interval was narrower than the 95% it claimed. True values:
    true = {31: 2.0395, 35: 2.0301, 45: 2.0141, 70: 1.9944, 150: 1.9759}
    from wololo_evals.stats import t_critical_95

    for df, want in true.items():
        assert t_critical_95(df) >= want - 1e-3, df
    assert t_critical_95(10_000) == pytest.approx(1.96, abs=0.001)
