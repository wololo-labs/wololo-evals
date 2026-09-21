"""``gate`` A/B verdict + ``core.ab`` statistics (SPEC README §16.2–§16.4).

Proves: paired-bootstrap CI upper bound drives the regression rule against the
tolerance-shifted null; a newly failing hard gate fails regardless of
significance; quarantined cases are excluded from the verdict but reported;
four-bucket classification; and the judge-input guards (judge_disabled reject,
uncalibrated refusal, calibration unlocks gating).

Pure: run trees are hand-written JSON at the on-disk schema ``gate`` reads
(``core.interfaces.artefacts`` layout); no Postgres, no engine, no network.
"""
import json
import pathlib

import pytest

from wololo_evals.cli import gate as gate_mod
from wololo_evals.cli.main import main
from wololo_evals.core import ab


# --------------------------------------------------------------------------
# run-tree scaffolding at the §15 schema gate reads
# --------------------------------------------------------------------------
def _scorecard(combo, cases):
    return {
        "schema_version": "2.1.0",
        "combo": {k: combo[k] for k in ("combo_id", "engine_id", "model_id", "prompt_id")},
        "judge_disabled": combo.get("judge_disabled", False),
        "summary": {"benchmark_score": sum(c["score"] for c in cases) / max(1, len(cases))},
        "cases": cases,
    }


def _write_run(run_dir, *, evaluation_type, combos, pass_threshold=0.7, benchmark="benchmarks/demo"):
    """combos: list of dicts {combo_id, engine_id, model_id, prompt_id, cases, [judge_disabled]}."""
    run_dir = pathlib.Path(run_dir)
    combo_records = []
    for combo in combos:
        combo_dir = f"combos/{combo['combo_id']}"
        sc_rel = f"{combo_dir}/runs/r/scorecard.json"
        sc_path = run_dir / sc_rel
        sc_path.parent.mkdir(parents=True, exist_ok=True)
        sc_path.write_text(json.dumps(_scorecard(combo, combo["cases"])), encoding="utf-8")
        combo_records.append(
            {
                "combo_id": combo["combo_id"],
                "engine_id": combo["engine_id"],
                "model_id": combo["model_id"],
                "prompt_id": combo["prompt_id"],
                "asset_id": combo.get("asset_id"),
                "role": "treatment",
                "combo_dir": combo_dir,
                "scorecard_path": sc_rel,
            }
        )
    summary = {
        "schema_version": "2.0.0",
        "matrix_id": run_dir.name,
        "evaluation_type": evaluation_type,
        "pass_threshold": pass_threshold,
        "benchmark": benchmark,
        "benchmark_id": "demo",
        "combos": combo_records,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "matrix_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run_dir


def _case(case_id, score, *, quarantined=False, hard_fail=False, judge=False):
    signals = [
        {"evaluator_id": "outcome", "kind": "assertion", "score": score,
         "weight": 1.0, "verdict": "pass" if score >= 0.7 else "fail", "slot": "task_outcome"}
    ]
    if judge:
        signals.append({"evaluator_id": "j", "kind": "judge", "score": score,
                        "weight": 1.0, "verdict": "pass", "slot": "llm_judge_overall"})
    if hard_fail:
        signals.append({"evaluator_id": "exfiltration", "kind": "safety", "score": 0.0,
                        "weight": 1.0, "verdict": "fail", "slot": None})
    return {
        "case_id": case_id, "score": 0.0 if hard_fail else score,
        "pass_rate": 0.0 if (hard_fail or score < 0.7) else 1.0,
        "quarantined": quarantined, "signals": signals,
    }


def _combo(cases, *, cid="claude__sonnet__default", judge_disabled=False):
    return {
        "combo_id": cid, "engine_id": "claude", "model_id": "sonnet",
        "prompt_id": "default", "cases": cases, "judge_disabled": judge_disabled,
    }


# --------------------------------------------------------------------------
# core.ab unit coverage
# --------------------------------------------------------------------------
def test_classify_four_buckets():
    assert ab.classify_case(True, True) == "working"
    assert ab.classify_case(False, True) == "fixed"
    assert ab.classify_case(True, False) == "regression"
    assert ab.classify_case(False, False) == "still_gap"


def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    deltas = [-0.2, -0.25, -0.15, -0.3, -0.22, -0.18, -0.27, -0.2, -0.24, -0.19]
    lo, hi = ab.paired_bootstrap_ci(deltas, resamples=2000, seed=7)
    assert lo <= sum(deltas) / len(deltas) <= hi
    assert (lo, hi) == ab.paired_bootstrap_ci(deltas, resamples=2000, seed=7)  # stable
    assert hi < 0  # a clear negative shift keeps the whole interval below zero


def test_bootstrap_rejects_thin_resamples():
    with pytest.raises(ValueError):
        ab.paired_bootstrap_ci([0.1, 0.2], resamples=500)


def test_wilson_interval_bounds():
    lo, hi = ab.wilson_interval(8, 10)
    assert 0.0 <= lo < 0.8 < hi <= 1.0
    assert ab.wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = ab.wilson_interval(10, 10)
    assert hi == 1.0 and lo > 0.7


def test_compare_excludes_quarantined_from_stats_and_buckets():
    pairs = [
        ab.CasePair("a", 1.0, 1.0, True, True),
        ab.CasePair("q", 1.0, 0.0, True, False, quarantined=True),
    ]
    v = ab.compare(pairs)
    assert v.n_quarantined == 1
    assert v.buckets == {"working": 1, "fixed": 0, "regression": 0, "still_gap": 0}
    assert not v.regressed


# --------------------------------------------------------------------------
# gate verdicts
# --------------------------------------------------------------------------
def _pair(tmp_path, cand_cases, base_cases, *, evaluation_type="code_change", **run_kw):
    run = _write_run(tmp_path / "cand", evaluation_type=evaluation_type,
                     combos=[_combo(cand_cases)], **run_kw)
    base = _write_run(tmp_path / "base", evaluation_type=evaluation_type,
                      combos=[_combo(base_cases)], **run_kw)
    return run, base


def test_gate_passes_when_no_regression(tmp_path):
    ids = [f"c{i}" for i in range(8)]
    cand = [_case(cid, 0.9) for cid in ids]
    base = [_case(cid, 0.9) for cid in ids]
    run, baseline = _pair(tmp_path, cand, base)
    v = gate_mod.evaluate(run, baseline)
    assert v.verdict == "pass"
    assert v.exit_code == 0
    assert v.ab_verdict.buckets["working"] == 8


def test_gate_fails_on_significant_regression(tmp_path):
    ids = [f"c{i}" for i in range(10)]
    cand = [_case(cid, 0.4) for cid in ids]   # all drop below the bar
    base = [_case(cid, 0.95) for cid in ids]
    run, baseline = _pair(tmp_path, cand, base)
    v = gate_mod.evaluate(run, baseline)
    assert v.verdict == "regression"
    assert v.exit_code == 1
    assert v.ab_verdict.significant_regression
    assert v.ab_verdict.ci_high < 0
    assert v.ab_verdict.buckets["regression"] == 10


def test_max_regression_tolerance_shifts_the_null(tmp_path):
    ids = [f"c{i}" for i in range(12)]
    cand = [_case(cid, 0.87) for cid in ids]   # uniform -0.03 delta, still passing
    base = [_case(cid, 0.90) for cid in ids]
    run, baseline = _pair(tmp_path, cand, base)
    # zero tolerance: a -0.03 shift is a significant regression
    assert gate_mod.evaluate(run, baseline, max_regression=0.0).verdict == "regression"
    # generous tolerance: within the shifted null, no regression
    assert gate_mod.evaluate(run, baseline, max_regression=0.05).verdict == "pass"


def test_newly_failing_hard_gate_fails_regardless_of_significance(tmp_path):
    ids = [f"c{i}" for i in range(6)]
    cand = [_case(cid, 0.95) for cid in ids]
    base = [_case(cid, 0.95) for cid in ids]
    cand[0] = _case("c0", 0.95, hard_fail=True)  # exfiltration trips on candidate only
    run, baseline = _pair(tmp_path, cand, base)
    v = gate_mod.evaluate(run, baseline)
    assert v.verdict == "regression"
    assert not v.ab_verdict.significant_regression  # not driven by the CI
    assert "claude__sonnet__default:c0" in v.ab_verdict.newly_failing_hard_gates


def test_quarantined_case_excluded_from_verdict(tmp_path):
    ids = [f"c{i}" for i in range(8)]
    cand = [_case(cid, 0.95) for cid in ids]
    base = [_case(cid, 0.95) for cid in ids]
    cand[0] = _case("c0", 0.0, quarantined=True)  # catastrophic but quarantined
    base[0] = _case("c0", 0.95, quarantined=True)
    run, baseline = _pair(tmp_path, cand, base)
    v = gate_mod.evaluate(run, baseline)
    assert v.verdict == "pass"
    assert "claude__sonnet__default:c0" in v.quarantined_cases
    assert v.ab_verdict.n_quarantined == 1


# --------------------------------------------------------------------------
# judge-input guards (§16.4 / §17)
# --------------------------------------------------------------------------
def _judge_pair(tmp_path, *, cand_disabled=False, base_disabled=False):
    ids = [f"c{i}" for i in range(6)]
    cand = [_case(cid, 0.9, judge=True) for cid in ids]
    base = [_case(cid, 0.9, judge=True) for cid in ids]
    run = _write_run(tmp_path / "cand", evaluation_type="skill_behaviour",
                     combos=[_combo(cand, judge_disabled=cand_disabled)])
    baseline = _write_run(tmp_path / "base", evaluation_type="skill_behaviour",
                          combos=[_combo(base, judge_disabled=base_disabled)])
    return run, baseline


def test_gate_refuses_uncalibrated_judge(tmp_path):
    run, baseline = _judge_pair(tmp_path)
    v = gate_mod.evaluate(run, baseline)  # no calibration record, no override
    assert v.verdict == "refused"
    assert v.exit_code == 2
    assert "uncalibrated" in v.reason
    assert v.judge["participates"] is True and v.judge["calibrated"] is False


def test_gate_unlocks_with_injected_agreement(tmp_path):
    run, baseline = _judge_pair(tmp_path)
    v = gate_mod.evaluate(run, baseline, golden_agreement=0.85)
    assert v.verdict == "pass"
    assert v.judge["calibrated"] is True


def test_gate_discovers_calibration_record(tmp_path):
    run, baseline = _judge_pair(tmp_path)
    bench = tmp_path / "benchmarks" / "demo"
    cal = bench / "labels" / "calibration"
    cal.mkdir(parents=True)
    (cal / "20260101T000000Z.json").write_text(json.dumps({"kappa": 0.5, "ts": "2026-01-01"}))
    (cal / "20260201T000000Z.json").write_text(json.dumps({"kappa": 0.82, "ts": "2026-02-01"}))
    v = gate_mod.evaluate(run, baseline, benchmark_dir=bench)  # newest κ=0.82 wins
    assert v.verdict == "pass"
    assert v.judge["agreement"] == 0.82


def test_gate_rejects_judge_disabled_runs(tmp_path):
    run, baseline = _judge_pair(tmp_path, cand_disabled=True)
    v = gate_mod.evaluate(run, baseline, golden_agreement=0.9)
    assert v.verdict == "refused"
    assert "judge_disabled" in v.reason


# --------------------------------------------------------------------------
# CLI end-to-end (main, exit codes, artefacts)
# --------------------------------------------------------------------------
def test_gate_cli_writes_verdict_json_and_exit_code(tmp_path, capsys):
    ids = [f"c{i}" for i in range(10)]
    cand = [_case(cid, 0.4) for cid in ids]
    base = [_case(cid, 0.95) for cid in ids]
    run, baseline = _pair(tmp_path, cand, base)
    out = tmp_path / "verdict.json"
    code = main(["gate", "--run", str(run), "--baseline", str(baseline), "-o", str(out)])
    assert code == 1
    payload = json.loads(out.read_text())
    assert payload["verdict"] == "regression"
    assert payload["stats"]["buckets"]["regression"] == 10
    md = capsys.readouterr().out
    assert "Gate verdict: REGRESSION" in md


def test_gate_records_the_asset_it_measured(tmp_path):
    # A verdict published beside an asset has to name that asset and the
    # commit it was measured at, or a catalogue reading it cannot tell whose
    # score it is, nor whether the score has gone stale.
    ids = [f"c{i}" for i in range(10)]
    run, baseline = _pair(
        tmp_path, [_case(c, 0.95) for c in ids], [_case(c, 0.4) for c in ids]
    )
    out = tmp_path / "verdict.json"
    main([
        "gate", "--run", str(run), "--baseline", str(baseline), "-o", str(out),
        "--subject-repo", "https://github.com/example/toolkit",
        "--subject-path", "packages/demo-pack",
        "--subject-commit", "0123456789abcdef0123456789abcdef01234567",
    ])
    assert json.loads(out.read_text())["subject"] == {
        "repo": "https://github.com/example/toolkit",
        "path": "packages/demo-pack",
        "commit": "0123456789abcdef0123456789abcdef01234567",
    }

    # unnamed subject stays absent rather than empty: nothing to mis-read
    plain = tmp_path / "plain.json"
    main(["gate", "--run", str(run), "--baseline", str(baseline), "-o", str(plain)])
    assert "subject" not in json.loads(plain.read_text())


def test_gate_report_carries_what_a_catalogue_publishes(tmp_path):
    # The report is published verbatim beside the asset, so everything a
    # catalogue renders has to come out of the gate rather than a transcription.
    ids = [f"c{i}" for i in range(10)]
    run, baseline = _pair(
        tmp_path, [_case(c, 0.95) for c in ids], [_case(c, 0.4) for c in ids]
    )
    out = tmp_path / "eval-gate-report.json"
    main([
        "gate", "--run", str(run), "--baseline", str(baseline), "-o", str(out),
        "--source", "https://github.com/example/evals/blob/abc123/README.md",
        "--source-visibility", "internal",
        "--recorded", "2026-09-21",
    ])
    payload = json.loads(out.read_text())
    assert payload["recorded"] == "2026-09-21"
    assert payload["sourceVisibility"] == "internal"
    assert payload["source"].endswith("/abc123/README.md")
    # the metrics a badge reads, straight off the signal rows
    assert {"evaluator_id", "candidate_mean", "baseline_mean"} <= payload["signals"][0].keys()

    # an undeclared date still records when the gate ran, never nothing
    dated = tmp_path / "dated.json"
    main(["gate", "--run", str(run), "--baseline", str(baseline), "-o", str(dated)])
    assert len(json.loads(dated.read_text())["recorded"]) == len("2026-09-21")


def test_gate_missing_summary_fails_loud(tmp_path):
    (tmp_path / "cand").mkdir()
    (tmp_path / "base").mkdir()
    with pytest.raises(gate_mod.GateError):
        gate_mod.evaluate(tmp_path / "cand", tmp_path / "base")


# --------------------------------------------------------------------------
# an evaluator that could not run must not read as a regression
# --------------------------------------------------------------------------
def _sig(evaluator_id, score, verdict="pass", slot="task_outcome"):
    return {
        "evaluator_id": evaluator_id, "kind": "assertion", "score": score,
        "weight": 1.0, "verdict": verdict, "slot": slot,
    }


def _write_arm(root, *, signals_by_case, benchmark_id="qe-skill"):
    """Minimal matrix tree: matrix_summary.json plus one combo scorecard."""
    combo_dir = root / "combos" / "c1" / "runs" / "c1"
    combo_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        {
            "case_id": case_id, "status": "ok", "score": score, "pass_rate": 1.0,
            "case_weight": 1.0, "quarantined": False, "signals": signals,
        }
        for case_id, (score, signals) in signals_by_case.items()
    ]
    (combo_dir / "scorecard.json").write_text(json.dumps({
        "schema_version": "2.1.0", "benchmark_id": benchmark_id,
        "summary": {"benchmark_score": sum(c["score"] for c in cases) / len(cases)},
        "cases": cases,
    }), encoding="utf-8")
    (root / "matrix_summary.json").write_text(json.dumps({
        "schema_version": "2.0.0", "benchmark_id": benchmark_id,
        "evaluation_type": "skill_behaviour", "pass_threshold": 0.7,
        "combos": [{
            "combo_id": "c1", "engine_id": "replay", "model_id": "m",
            "prompt_id": "p",
            "scorecard_path": "combos/c1/runs/c1/scorecard.json",
        }],
    }), encoding="utf-8")
    return root


def test_a_scorer_outage_on_one_arm_is_not_a_regression(tmp_path):
    """A mutmut outage in the candidate arm must not average in as 0.0 and
    report the candidate getting worse: the tool broke, the candidate did not."""
    baseline = _write_arm(tmp_path / "base", signals_by_case={
        "c1": (1.0, [_sig("kill_rate", 0.24), _sig("other", 1.0)]),
        "c2": (1.0, [_sig("kill_rate", 0.26), _sig("other", 1.0)]),
    })
    candidate = _write_arm(tmp_path / "cand", signals_by_case={
        "c1": (1.0, [_sig("kill_rate", 0.40), _sig("other", 1.0)]),
        # The scorer fell over on the second case only.
        "c2": (1.0, [_sig("kill_rate", 0.0, "evaluator_error"), _sig("other", 1.0)]),
    })

    verdict = gate_mod.evaluate(candidate, baseline, signal_guards=("kill_rate",))
    delta = next(d for d in verdict.signal_deltas if d.evaluator_id == "kill_rate")

    # 0.40 alone, not (0.40 + 0.0) / 2 = 0.20 against a baseline of 0.25.
    assert delta.candidate_mean == pytest.approx(0.40)
    assert delta.candidate_n == 1
    assert delta.candidate_errors == 1
    assert not delta.regressed
    assert verdict.verdict == "pass"
    assert verdict.exit_code == 0


def test_a_guard_on_an_error_only_signal_refuses(tmp_path):
    """Refusing (exit 2) is the honest outcome: the guard measured nothing, so
    the run is neither a pass nor evidence of a regression."""
    baseline = _write_arm(tmp_path / "base", signals_by_case={
        "c1": (1.0, [_sig("kill_rate", 0.24), _sig("other", 1.0)]),
    })
    candidate = _write_arm(tmp_path / "cand", signals_by_case={
        "c1": (1.0, [_sig("kill_rate", 0.0, "evaluator_error"), _sig("other", 1.0)]),
    })

    verdict = gate_mod.evaluate(candidate, baseline, signal_guards=("kill_rate",))
    assert verdict.verdict == "refused"
    assert verdict.exit_code == 2
    assert "error-only in run" in verdict.reason

    # Unguarded, the same pair is simply not compared on that signal.
    ungated = gate_mod.evaluate(candidate, baseline)
    assert ungated.verdict == "pass"
    assert "kill_rate" not in {d.evaluator_id for d in ungated.signal_deltas}


def test_a_guard_on_an_absent_signal_still_refuses(tmp_path):
    baseline = _write_arm(tmp_path / "base", signals_by_case={
        "c1": (1.0, [_sig("other", 1.0)]),
    })
    candidate = _write_arm(tmp_path / "cand", signals_by_case={
        "c1": (1.0, [_sig("other", 1.0)]),
    })
    verdict = gate_mod.evaluate(candidate, baseline, signal_guards=("kill_rate",))
    assert verdict.exit_code == 2
    assert "absent" in verdict.reason
