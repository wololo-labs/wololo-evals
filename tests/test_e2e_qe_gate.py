"""End-to-end QE gate proof: the Slice C success metric in one test.

With **zero model spend**, drive the whole spine over the committed
``examples/runs/qe-quoting`` corpus and the ``examples/fixtures/quoting-service``
fixture:

1. **compile** ``benchmarks/qe-skill/cases.toml`` and confirm the committed
   ``cases.jsonl`` is in sync (§11.1);
2. **matrix** both arms through the offline ``ReplayAdapter`` with the
   **default CLI seams** - the real production grader, the real replay
   normaliser, no injected fakes - and assert both arms score above zero, so
   the scoreless-default regression cannot come back;
3. assert the mechanical signals are *computed*: the mutation kill rate, the
   seeded-defect catch rate and the ``qe-review`` rubric all land as separate
   signals with real details, and the candidate beats the baseline on kill rate
   while flake resistance holds;
4. **gate**: candidate-vs-baseline exits 0, and a guard on a signal that did
   regress turns the same pair into exit 1;
5. the committed ``recorded/`` pair reproduces that verdict with no
   recomputation, which is what a fresh clone runs.

Marked ``mutation``: this drives the real mutmut-backed scorer over twelve
cases and takes about two minutes, which is the price of proving the numbers
are not recordings.

Pure: ``tmp_path`` output dirs, no Postgres, no engine subprocess, no network,
no model.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from wololo_evals.cli import cases_compile
from wololo_evals.cli import gate as gate_mod
from wololo_evals.cli.main import main
from wololo_evals.scoring.mutation import MUTATION_EVALUATOR_ID
from wololo_evals.scoring.defects import DEFECT_EVALUATOR_ID
from wololo_evals.scoring.qe_rubric import QE_RUBRIC_EVALUATOR_ID

_REPO = pathlib.Path(__file__).resolve().parents[1]
_BENCH = _REPO / "benchmarks" / "qe-skill"
_CORPUS = _REPO / "examples" / "runs" / "qe-quoting"
_RECORDED = _CORPUS / "recorded"

_N_CASES = 12


def _has_mutmut() -> bool:
    try:
        import mutmut  # noqa: F401
    except ImportError:
        return False
    return True


needs_mutmut = pytest.mark.skipif(
    not _has_mutmut(),
    reason="mutmut is not installed (pip install 'wololo-evals[mutation]')",
)


def _write_config(tmp_path: pathlib.Path, arm: str) -> pathlib.Path:
    """A matrix config rooted at tmp_path, pointing back at the committed
    benchmark and archive. Only ``output_dir`` differs from the committed one,
    so this exercises the same wiring an operator runs."""
    config = tmp_path / f"matrix-{arm}.toml"
    runs_dir = _CORPUS / ("baseline-noagent" if arm == "baseline" else "candidate")
    config.write_text(
        f"""
schema_version = "2.0.0"
benchmark = "{_BENCH}"
output_dir = "out-{arm}"
seeds = 1

[engines.replay]
version_range = "*"
runs_dir = "{runs_dir}"

[[models]]
id = "qe-recorded"
engine = "replay"
model = "claude-sonnet-4-5"

[[prompts]]
id = "qe-skill"
""",
        encoding="utf-8",
    )
    return config


def _matrix_dir(tmp_path: pathlib.Path, arm: str) -> pathlib.Path:
    (root,) = (tmp_path / f"out-{arm}" / "iterations" / "001").iterdir()
    return root


def _scorecard(matrix_dir: pathlib.Path) -> dict:
    summary = json.loads((matrix_dir / "matrix_summary.json").read_text())
    (combo,) = summary["combos"]
    return json.loads((matrix_dir / combo["scorecard_path"]).read_text())


def _signals(scorecard: dict, case_id: str) -> dict[str, dict]:
    case = next(c for c in scorecard["cases"] if c["case_id"] == case_id)
    return {s["evaluator_id"]: s for s in case["signals"]}


# --------------------------------------------------------------------------
# 1. the committed casebook is in sync
# --------------------------------------------------------------------------
def test_the_committed_cases_jsonl_is_in_sync():
    ok, message = cases_compile.check(_BENCH / "cases.toml")
    assert ok, message
    cases = cases_compile.load_cases(_BENCH / "cases.jsonl")
    assert len(cases) == _N_CASES
    distractors = [c for c in cases if c.role == "distractor"]
    assert len(distractors) == 1
    assert distractors[0].expected_outcome == "no_activation"


def test_every_treatment_case_declares_its_qe_target():
    for case in cases_compile.load_cases(_BENCH / "cases.jsonl"):
        qe = case.metadata["qe"]
        if case.role == "distractor":
            assert qe["enabled"] is False
            continue
        for key in ("target", "tests", "changed_lines_file", "defects_file"):
            assert qe[key], f"{case.case_id} declares no {key}"


# --------------------------------------------------------------------------
# 2-4. the full spine
# --------------------------------------------------------------------------
@needs_mutmut
@pytest.mark.mutation
def test_the_qe_gate_runs_end_to_end_offline(tmp_path, monkeypatch):
    # No judge configured: the documented zero-spend path.
    monkeypatch.setenv("WOLOLO_EVALS_JUDGE", "off")

    # --- 2. both arms, through the default CLI seams --------------------
    for arm in ("baseline", "candidate"):
        assert main(["matrix", "--config", str(_write_config(tmp_path, arm))]) == 0

    baseline_dir = _matrix_dir(tmp_path, "baseline")
    candidate_dir = _matrix_dir(tmp_path, "candidate")
    baseline = _scorecard(baseline_dir)
    candidate = _scorecard(candidate_dir)

    # The regression this whole slice exists to close: with the scoreless
    # default every one of these was exactly 0.
    assert baseline["summary"]["benchmark_score"] > 0.0
    assert candidate["summary"]["benchmark_score"] > baseline["summary"]["benchmark_score"]
    assert len(candidate["cases"]) == _N_CASES

    # Spend really is zero: the replay engine never calls a model.
    assert json.loads((candidate_dir / "matrix_summary.json").read_text())[
        "cumulative_spend_usd"
    ] == 0.0

    # --- 3. the mechanical signals are computed, and separate ------------
    signals = _signals(candidate, "qe-01-weight-boundary")
    assert {MUTATION_EVALUATOR_ID, DEFECT_EVALUATOR_ID, QE_RUBRIC_EVALUATOR_ID} <= set(signals)

    mutation = signals[MUTATION_EVALUATOR_ID]
    assert mutation["details"]["total"] > 50  # real mutants, not a recorded number
    assert 0.0 < mutation["score"] < 1.0

    defects = signals[DEFECT_EVALUATOR_ID]
    assert "non-positive-weight-accepted" in defects["details"]["caught"]

    rubric = signals[QE_RUBRIC_EVALUATOR_ID]
    assert set(rubric["details"]["dimensions"]) == {
        "assertion_strength", "flake_resistance",
        "changed_line_coverage", "no_tautological_assertions",
    }
    assert len(rubric["details"]["rubric_version"]) == 64  # content-hashed

    # The headline case caught the planted defect; the baseline arm did not.
    assert signals["catches_the_weight_defect"]["verdict"] == "pass"
    assert _signals(baseline, "qe-01-weight-boundary")[
        "catches_the_weight_defect"
    ]["verdict"] == "fail"

    # The distractor scores by staying silent, in both arms.
    for arm in (baseline, candidate):
        activation = _signals(arm, "qe-12-distractor-licence")["used_qe_skill"]
        assert activation["slot"] == "activation_correct"
        assert activation["verdict"] == "pass"

    # --- 4. gate --------------------------------------------------------
    verdict = gate_mod.evaluate(
        candidate_dir, baseline_dir,
        signal_guards=("mutation_kill_rate", "qe_flake_resistance",
                       "seeded_defects_caught"),
    )
    assert verdict.verdict == "pass"
    assert verdict.exit_code == 0
    assert verdict.ab_verdict.significant_improvement

    by_id = {d.evaluator_id: d for d in verdict.signal_deltas}
    assert by_id["mutation_kill_rate"].delta > 0, "the candidate must kill more mutants"
    assert by_id["seeded_defects_caught"].delta > 0
    assert not by_id["qe_flake_resistance"].regressed
    assert not verdict.guarded_regressions

    # A guard on a signal that *did* slip turns the same pair into exit 1, so
    # the guard is doing work rather than decorating a verdict that passes.
    slipped = next(d.evaluator_id for d in verdict.signal_deltas if d.regressed)
    guarded = gate_mod.evaluate(candidate_dir, baseline_dir, signal_guards=(slipped,))
    assert guarded.verdict == "regression"
    assert guarded.exit_code == 1
    assert guarded.guarded_regressions == (slipped,)


def test_a_guard_naming_an_absent_signal_refuses():
    verdict = gate_mod.evaluate(
        _RECORDED / "candidate", _RECORDED / "baseline",
        signal_guards=("no_such_signal",),
    )
    assert verdict.verdict == "refused"
    assert verdict.exit_code == 2


# --------------------------------------------------------------------------
# 5. the committed pair reproduces the verdict with no recomputation
# --------------------------------------------------------------------------
def test_the_recorded_pair_gates_green_from_a_fresh_clone(tmp_path):
    out = tmp_path / "verdict.json"
    exit_code = main([
        "gate",
        "--run", str(_RECORDED / "candidate"),
        "--baseline", str(_RECORDED / "baseline"),
        "--signal-guard", "mutation_kill_rate",
        "--signal-guard", "qe_flake_resistance",
        "--signal-guard", "seeded_defects_caught",
        "-o", str(out),
    ])
    assert exit_code == 0

    payload = json.loads(out.read_text())
    assert payload["verdict"] == "pass"
    assert payload["stats"]["significant_improvement"]
    assert payload["guarded_regressions"] == []

    signals = {s["evaluator_id"]: s for s in payload["signals"]}
    # Criterion: kill rate and the QE rubric are separate signals, the
    # candidate beats baseline on kill rate, flake resistance does not regress.
    assert signals[MUTATION_EVALUATOR_ID]["delta"] > 0
    assert signals[QE_RUBRIC_EVALUATOR_ID]["delta"] > 0
    assert signals[MUTATION_EVALUATOR_ID] is not signals[QE_RUBRIC_EVALUATOR_ID]
    assert not signals["qe_flake_resistance"]["regressed"]


def test_the_recorded_pair_carries_no_absolute_paths():
    """A corpus that baked its author's home directory in would gate only on
    their machine."""
    for path in _RECORDED.rglob("*.json"):
        assert "/Users/" not in path.read_text(encoding="utf-8")
        assert "/home/" not in path.read_text(encoding="utf-8")
