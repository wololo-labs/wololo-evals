"""Contract tests for ``core.interfaces`` (wave-2 binding surface).

Proves the three invariants the wave-contracts doc pins on this shared module
(``docs/plans/wave-contracts.md`` — "Test: tests/test_core_interfaces.py"):

1. **Fail-loud ``__post_init__``** — every frozen record rejects a malformed
   value at construction rather than letting it silently poison a downstream
   aggregate (README §11.1/§14.2/§15.3). A builder that mis-populates a slot
   crashes at the record boundary, not three layers deep in scoring.
2. **``artefacts.*`` path shapes match README §15** — the matrix layout has one
   definition; every builder that reads/writes the tree derives paths from this
   namespace, so a change to the layout is a change here (and this test).
3. **``SuiteDefinition.judged_dimensions()`` filters on ``scorer``** — the
   k-sample judge slot list is driven purely by ``DimensionSpec.scorer ==
   "judge"`` so a new suite needs no engine change.

Pure/in-process (no Postgres): the module imports with no database, so these
follow ``test_stats.py`` — plain values in, asserted invariants out, no
fixtures.
"""
from __future__ import annotations

import pathlib

import pytest

from wololo_evals.core import interfaces
from wololo_evals.core.interfaces import (
    ADVISORY_DIR,
    AssetRef,
    AssetSnapshot,
    CaseSpec,
    DimensionSpec,
    RunCaps,
    RunResult,
    RunSpec,
    ScoredSignal,
    SuiteDefinition,
    TraceEvent,
    artefacts,
)


# ---------------------------------------------------------------------------
# Small valid builders — the happy-path each fail-loud test mutates one field of
# ---------------------------------------------------------------------------
def _asset_ref() -> AssetRef:
    return AssetRef(kind="agent_skill", id="skill-1")


def _snapshot() -> AssetSnapshot:
    return AssetSnapshot(
        ref=_asset_ref(),
        body="skill body",
        files={"SKILL.md": "skill body"},
        adapter_id="agent_skill",
        snapshot_digest="deadbeef",
    )


def _case() -> CaseSpec:
    return CaseSpec(case_id="case-1")


def _run_spec() -> RunSpec:
    return RunSpec(
        run_id="run-1",
        asset=_snapshot(),
        case=_case(),
        engine_id="claude",
        model_id="claude-x",
        prompt_id="p-1",
        seed=0,
        cwd="/tmp/ws",
    )


# ---------------------------------------------------------------------------
# 1. Fail-loud __post_init__ — valid construction then each rejection
# ---------------------------------------------------------------------------
def test_asset_ref_valid_and_rejections():
    ref = _asset_ref()
    assert ref.kind == "agent_skill"
    # frozen: metadata defaults to an independent empty mapping
    assert ref.metadata == {}

    with pytest.raises(ValueError, match="AssetRef.kind"):
        AssetRef(kind="not_a_kind", id="x")
    with pytest.raises(ValueError, match="AssetRef.id is required"):
        AssetRef(kind="agent_skill", id="")


def test_case_spec_valid_and_rejections():
    case = _case()
    assert case.role == "treatment"
    assert case.expected_outcome == "pass"

    with pytest.raises(ValueError, match="CaseSpec.case_id is required"):
        CaseSpec(case_id="")
    with pytest.raises(ValueError, match="CaseSpec.role"):
        CaseSpec(case_id="c", role="bystander")
    with pytest.raises(ValueError, match="CaseSpec.expected_outcome"):
        CaseSpec(case_id="c", expected_outcome="maybe")
    # cross-field rule: a distractor must expect no activation
    with pytest.raises(ValueError, match="distractor cases require"):
        CaseSpec(case_id="c", role="distractor", expected_outcome="pass")
    # the same combination with the right outcome is accepted
    ok = CaseSpec(case_id="c", role="distractor", expected_outcome="no_activation")
    assert ok.role == "distractor"


def test_run_spec_valid_and_rejections():
    spec = _run_spec()
    assert spec.engine_id == "claude"
    assert isinstance(spec.caps, RunCaps)

    with pytest.raises(ValueError, match="RunSpec.run_id is required"):
        RunSpec(
            run_id="",
            asset=_snapshot(),
            case=_case(),
            engine_id="claude",
            model_id="m",
            prompt_id="p",
            seed=0,
            cwd="/tmp",
        )
    with pytest.raises(ValueError, match="RunSpec.engine_id"):
        RunSpec(
            run_id="r",
            asset=_snapshot(),
            case=_case(),
            engine_id="gemini",
            model_id="m",
            prompt_id="p",
            seed=0,
            cwd="/tmp",
        )
    with pytest.raises(ValueError, match="RunSpec.seed must be >= 0"):
        RunSpec(
            run_id="r",
            asset=_snapshot(),
            case=_case(),
            engine_id="claude",
            model_id="m",
            prompt_id="p",
            seed=-1,
            cwd="/tmp",
        )


def test_run_result_status_taxonomy_and_scored_property():
    ok = RunResult(run_id="r", status="ok", artefact_dir="d", runtime_seconds=1.0)
    assert ok.scored is True
    # every non-ok status is a visible zero, not a graded row (§14.2)
    for status in ("model_fail", "infra_fail", "timeout", "caps_exceeded", "cost_cap_hit"):
        row = RunResult(run_id="r", status=status, artefact_dir="d", runtime_seconds=1.0)
        assert row.scored is False

    with pytest.raises(ValueError, match="RunResult.status"):
        RunResult(run_id="r", status="exploded", artefact_dir="d", runtime_seconds=1.0)
    with pytest.raises(ValueError, match="RunResult.attempts must be >= 1"):
        RunResult(
            run_id="r", status="ok", artefact_dir="d", runtime_seconds=1.0, attempts=0
        )


def test_trace_event_valid_rejections_and_skill_activation():
    evt = TraceEvent(seq=0, ts_ms=10, kind="tool_call", name="Read", target="a.py")
    assert evt.is_skill_activation is False

    activation = TraceEvent(
        seq=1,
        ts_ms=20,
        kind="lifecycle",
        name=interfaces.SKILL_ACTIVATED,
        target="skill-1",
    )
    assert activation.is_skill_activation is True
    # right verb, wrong kind is not an activation
    not_lifecycle = TraceEvent(
        seq=2, ts_ms=30, kind="message", name=interfaces.SKILL_ACTIVATED
    )
    assert not_lifecycle.is_skill_activation is False

    with pytest.raises(ValueError, match="TraceEvent.kind"):
        TraceEvent(seq=0, ts_ms=0, kind="not_a_kind")
    with pytest.raises(ValueError, match="TraceEvent.seq must be >= 0"):
        TraceEvent(seq=-1, ts_ms=0, kind="message")


def test_scored_signal_valid_and_rejections():
    sig = ScoredSignal(
        evaluator_id="e",
        kind="judge",
        score=0.8,
        weight=1.0,
        verdict="pass",
        slot="llm_judge_overall",
    )
    assert sig.slot == "llm_judge_overall"

    with pytest.raises(ValueError, match="ScoredSignal.kind"):
        ScoredSignal(evaluator_id="e", kind="vibes", score=1.0, weight=1.0, verdict="pass")
    with pytest.raises(ValueError, match="ScoredSignal.verdict"):
        ScoredSignal(evaluator_id="e", kind="judge", score=1.0, weight=1.0, verdict="meh")
    with pytest.raises(ValueError, match="ScoredSignal.weight must be >= 0"):
        ScoredSignal(
            evaluator_id="e", kind="judge", score=1.0, weight=-0.1, verdict="pass"
        )


# ---------------------------------------------------------------------------
# 3. SuiteDefinition.judged_dimensions() filters on scorer
# ---------------------------------------------------------------------------
def test_suite_definition_validates_and_filters_judged_dimensions():
    suite = SuiteDefinition(
        suite_id="skill-behaviour",
        evaluation_type="skill_behaviour",
        dimensions=(
            DimensionSpec(name="activation", weight=0.25, scorer="transcript"),
            DimensionSpec(name="task-outcome", weight=0.45, scorer="acceptance-test"),
            DimensionSpec(name="trace-conformance", weight=0.30, scorer="judge"),
        ),
    )
    # only the scorer == "judge" dimension is a judge slot
    assert suite.judged_dimensions() == ("trace-conformance",)

    # a suite whose dimensions are all rule-based has an empty judge slot list
    no_judge = SuiteDefinition(
        suite_id="mechanical",
        evaluation_type="code_change",
        dimensions=(
            DimensionSpec(name="applies", weight=0.5, scorer="acceptance-test"),
            DimensionSpec(name="churn", weight=0.5, scorer="churn"),
        ),
    )
    assert no_judge.judged_dimensions() == ()

    # default scorer is judge, so an unannotated dimension counts
    default_judge = SuiteDefinition(
        suite_id="advisory",
        evaluation_type="advisory",
        dimensions=(DimensionSpec(name="overall", weight=1.0),),
    )
    assert default_judge.judged_dimensions() == ("overall",)


def test_suite_definition_rejections():
    dims = (DimensionSpec(name="d", weight=1.0),)
    with pytest.raises(ValueError, match="SuiteDefinition.evaluation_type"):
        SuiteDefinition(suite_id="s", evaluation_type="freeform", dimensions=dims)
    # judge_k must be a positive odd number (self-consistency needs a majority)
    with pytest.raises(ValueError, match="judge_k must be a positive odd number"):
        SuiteDefinition(
            suite_id="s", evaluation_type="advisory", dimensions=dims, judge_k=4
        )
    with pytest.raises(ValueError, match="judge_k must be a positive odd number"):
        SuiteDefinition(
            suite_id="s", evaluation_type="advisory", dimensions=dims, judge_k=0
        )
    # dimension weights must sum to 1.0
    with pytest.raises(ValueError, match="weights sum to"):
        SuiteDefinition(
            suite_id="s",
            evaluation_type="advisory",
            dimensions=(
                DimensionSpec(name="a", weight=0.3),
                DimensionSpec(name="b", weight=0.3),
            ),
        )


# ---------------------------------------------------------------------------
# 2. artefacts.* path shapes match README §15 matrix layout
# ---------------------------------------------------------------------------
def test_artefact_iteration_and_matrix_paths():
    out = pathlib.Path("/out")
    # iterations are zero-padded to three digits (§15 tree)
    assert artefacts.iteration_dir(out, 7) == out / "iterations" / "007"
    assert artefacts.snapshot_dir(out, 7) == out / "iterations" / "007" / "snapshot"
    assert artefacts.feedback_path(out, 7) == out / "iterations" / "007" / "feedback.json"

    matrix = artefacts.matrix_dir(out, 7, "20260719T0102Z")
    assert matrix == out / "iterations" / "007" / "matrix-20260719T0102Z"
    assert artefacts.matrix_summary_path(matrix) == matrix / "matrix_summary.json"
    assert artefacts.matrix_events_path(matrix) == matrix / "events.jsonl"


def test_artefact_combo_run_and_seed_paths():
    matrix = pathlib.Path("/out/iterations/007/matrix-t")
    combo = artefacts.combo_dir(matrix, "claude__m__p")
    assert combo == matrix / "combos" / "claude__m__p"
    assert artefacts.combo_cases_path(matrix, "claude__m__p") == combo / "cases.jsonl"

    run_dir = artefacts.combo_run_dir(matrix, "claude__m__p", "run-1")
    assert run_dir == combo / "runs" / "run-1"
    assert artefacts.scorecard_path(run_dir) == run_dir / "scorecard.json"
    assert artefacts.run_events_path(run_dir) == run_dir / "events.jsonl"
    assert artefacts.case_aggregate_path(run_dir, "case-1") == run_dir / "cases" / "case-1.json"

    # seed level rolls under the combo-run dir
    seed = artefacts.seed_dir(run_dir, 3)
    assert seed == run_dir / "seeds" / "3"
    assert artefacts.seed_case_path(run_dir, 3, "case-1") == seed / "cases" / "case-1.json"
    assert artefacts.workspace_dir(run_dir, 3, "case-1") == seed / "workspace" / "case-1"


def test_artefact_capture_files_live_under_the_advisory_dir():
    run_dir = pathlib.Path("/out/iterations/007/matrix-t/combos/c/runs/run-1")
    advisory = artefacts.advisory_dir(run_dir, 3, "case-1")
    assert advisory == artefacts.workspace_dir(run_dir, 3, "case-1") / ADVISORY_DIR

    # every capture file hangs off that one advisory dir (§14.5 / §15.2)
    assert artefacts.raw_event_path(run_dir, 3, "case-1", "claude") == advisory / "claude.raw.jsonl"
    assert artefacts.stdout_path(run_dir, 3, "case-1", "claude") == advisory / "claude.stdout.log"
    assert artefacts.stderr_path(run_dir, 3, "case-1", "claude") == advisory / "claude.stderr.log"
    assert artefacts.trace_path(run_dir, 3, "case-1") == advisory / "trace.jsonl"
    assert artefacts.prompt_path(run_dir, 3, "case-1") == advisory / "prompt.md"
    assert artefacts.advisory_json_path(run_dir, 3, "case-1") == advisory / "advisory.json"
    assert artefacts.grading_path(run_dir, 3, "case-1") == advisory / "grading.json"


def test_artefact_helpers_are_pure_no_io(tmp_path):
    # deriving a deep path must not touch the filesystem (no mkdir side effects)
    run_dir = tmp_path / "runs" / "run-1"
    _ = artefacts.raw_event_path(run_dir, 0, "case-1", "codex")
    assert not run_dir.exists()
