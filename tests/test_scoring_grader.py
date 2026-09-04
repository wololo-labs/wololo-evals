"""The production grader is the module that decides what a run scored, so its
dispatch table is the load-bearing invariant of the whole gate:

* **dispatch by kind**: a ``trace:`` assertion reaches the trajectory engine,
  a ``code:`` assertion reaches the acceptance runner, and a plain-text
  assertion reaches the judge; each emits a ``ScoredSignal`` on its §16.1 slot;
* **live-shaped runs score above zero**: the whole point of replacing the
  scoreless default, and a failing assertion still scores that case zero;
* **activation inversion**: on a distractor, staying silent is the pass;
* **fail loudly**: a malformed assertion is ``evaluator_error``, never a
  silent zero that reads as a bad candidate;
* **sandboxing**: a ``code:`` expression cannot reach past its namespace.

Pure: module-level fakes, ``tmp_path`` fixtures, no Postgres, no engine
subprocess, no network, no model.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from wololo_evals.cli.artefact_writer import compose_case
from wololo_evals.core import interfaces as ci
from wololo_evals.scoring import acceptance, build_grader
from wololo_evals.scoring.grader import ACTIVATION_SLOT, JUDGE_SLOT, assertion_kind

_TRACE = "trace:require_order(read, edit)"
_CODE = "code:output.tests_added >= 3"
_JUDGE = "the answer explains which boundary the new tests pin"


def _asset() -> ci.AssetSnapshot:
    return ci.AssetSnapshot(
        ref=ci.AssetRef(kind="agent_skill", id="qe-skill", inline_body=""),
        body="", files={}, adapter_id="local", snapshot_digest="0" * 64,
    )


def _case(
    *,
    case_id: str = "c1",
    assertions: tuple[dict, ...] = (),
    role: str = "treatment",
    expected_outcome: str = "pass",
    metadata: dict | None = None,
) -> ci.CaseSpec:
    return ci.CaseSpec(
        case_id=case_id,
        input_text="add boundary tests",
        role=role,
        expected_outcome=expected_outcome,
        expected_skill="qe-test-authoring",
        assertions=assertions,
        metadata=metadata or {},
    )


def _spec(case: ci.CaseSpec, cwd: pathlib.Path) -> ci.RunSpec:
    return ci.RunSpec(
        run_id=f"run-{case.case_id}", asset=_asset(), case=case,
        engine_id="claude", model_id="claude-sonnet-4-5", prompt_id="stock",
        seed=0, cwd=str(cwd),
    )


def _result(cwd: pathlib.Path) -> ci.RunResult:
    return ci.RunResult(
        run_id="run-c1", status="ok",
        artefact_dir=str(cwd / ci.ADVISORY_DIR), runtime_seconds=1.0,
    )


def _trace(*, activated: bool = True) -> list[ci.TraceEvent]:
    events = []
    seq = 0
    if activated:
        events.append(
            ci.TraceEvent(seq=seq, ts_ms=0, kind="lifecycle",
                          name=ci.SKILL_ACTIVATED, target="qe-test-authoring")
        )
        seq += 1
    events.append(
        ci.TraceEvent(seq=seq, ts_ms=1, kind="tool_call", name="read",
                      target="quoting/pricing.py")
    )
    events.append(
        ci.TraceEvent(seq=seq + 1, ts_ms=2, kind="tool_call", name="edit",
                      target="tests/test_boundary.py")
    )
    return events


def _write_output(cwd: pathlib.Path, payload: dict) -> None:
    adv = cwd / ci.ADVISORY_DIR
    adv.mkdir(parents=True, exist_ok=True)
    (adv / "output.json").write_text(json.dumps(payload), encoding="utf-8")


def _judge_backend(score: float = 0.9):
    """A judge stand-in: records what it was shown, returns a fixed score."""
    seen: list[tuple[str, str]] = []

    def backend(assertion_text: str, candidate_content: str) -> tuple[float, str]:
        seen.append((assertion_text, candidate_content))
        return score, f"fake judge scored {score}"

    backend.seen = seen  # type: ignore[attr-defined]
    return backend


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
def test_assertion_kind_classifies_the_three_forms():
    assert assertion_kind(_TRACE) == "trace"
    assert assertion_kind(_CODE) == "code"
    assert assertion_kind(_JUDGE) == "judge"


def test_each_assertion_kind_dispatches_to_its_engine(tmp_path):
    """One case carrying all three assertion forms yields three signals, on
    three different §16.1 slots, from three different engines."""
    _write_output(tmp_path, {"tests_added": 4, "answer": "pinned the zero-weight boundary"})
    judge = _judge_backend()
    grader = build_grader(judge_backend=judge, mutation_enabled=False)
    case = _case(assertions=(
        {"id": "ordered", "text": _TRACE, "weight": 1.0},
        {"id": "wrote_tests", "text": _CODE, "weight": 1.0},
        {"id": "explains", "text": _JUDGE, "weight": 1.0},
    ))

    signals = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    by_id = {s.evaluator_id: s for s in signals}
    assert set(by_id) == {"ordered", "wrote_tests", "explains"}

    # trace: -> trajectory engine -> trace_conformance
    assert by_id["ordered"].kind == "assertion"
    assert by_id["ordered"].slot == "trace_conformance"
    assert by_id["ordered"].verdict == "pass"

    # code: -> acceptance runner -> task_outcome, evidence names the expression
    assert by_id["wrote_tests"].kind == "assertion"
    assert by_id["wrote_tests"].slot == "task_outcome"
    assert by_id["wrote_tests"].verdict == "pass"
    assert "output.tests_added" in by_id["wrote_tests"].evidence

    # plain text -> judge -> llm_judge_overall, and the judge saw the candidate
    # content, never the assertion smuggled into the content slot (SPEC S8).
    assert by_id["explains"].kind == "judge"
    assert by_id["explains"].slot == JUDGE_SLOT
    assert by_id["explains"].score == pytest.approx(0.9)
    (assertion_text, candidate_content), = judge.seen
    assert assertion_text == _JUDGE
    assert "pinned the zero-weight boundary" in candidate_content


def test_a_live_shaped_run_scores_above_zero(tmp_path):
    """The regression this grader exists to close: with the scoreless default,
    every live matrix scored exactly 0."""
    _write_output(tmp_path, {"tests_added": 5, "answer": "done"})
    grader = build_grader(judge_backend=_judge_backend(), mutation_enabled=False)
    case = _case(assertions=(
        {"id": "activated", "text": "trace:used_skill(qe-test-authoring)", "weight": 1.0},
        {"id": "ordered", "text": _TRACE, "weight": 1.0},
        {"id": "wrote_tests", "text": _CODE, "weight": 1.0},
        {"id": "explains", "text": _JUDGE, "weight": 1.0},
    ))

    signals = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    composed = compose_case(signals, "skill_behaviour", pass_threshold=0.7)
    assert composed.score > 0.0
    assert composed.passed
    assert set(composed.slot_scores) == {
        ACTIVATION_SLOT, "trace_conformance", "task_outcome", JUDGE_SLOT
    }


def test_a_failing_assertion_scores_that_slot_zero(tmp_path):
    _write_output(tmp_path, {"tests_added": 1})
    grader = build_grader(mutation_enabled=False)
    case = _case(assertions=({"id": "wrote_tests", "text": _CODE, "weight": 1.0},))

    (signal,) = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    assert signal.verdict == "fail"
    assert signal.score == 0.0
    composed = compose_case([signal], "skill_behaviour", pass_threshold=0.7)
    assert composed.score == 0.0
    assert not composed.passed


# --------------------------------------------------------------------------
# activation semantics
# --------------------------------------------------------------------------
def test_distractor_scores_activation_by_staying_silent(tmp_path):
    grader = build_grader(mutation_enabled=False)
    case = _case(
        case_id="distractor", role="distractor", expected_outcome="no_activation",
        assertions=({"id": "activated", "text": "trace:used_skill(qe-test-authoring)",
                     "weight": 1.0},),
    )

    (silent,) = grader(_spec(case, tmp_path), _result(tmp_path), _trace(activated=False))
    assert silent.slot == ACTIVATION_SLOT
    assert silent.verdict == "pass"

    (fired,) = grader(_spec(case, tmp_path), _result(tmp_path), _trace(activated=True))
    assert fired.verdict == "fail"
    assert "activated anyway" in fired.evidence


def test_used_skill_feeds_activation_not_trace_conformance(tmp_path):
    grader = build_grader(mutation_enabled=False)
    case = _case(assertions=(
        {"id": "activated", "text": "trace:used_skill(qe-test-authoring)", "weight": 1.0},
    ))
    (signal,) = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    assert signal.slot == ACTIVATION_SLOT


# --------------------------------------------------------------------------
# failure discipline
# --------------------------------------------------------------------------
def test_a_malformed_assertion_is_an_evaluator_error_not_a_zero(tmp_path):
    grader = build_grader(mutation_enabled=False)
    case = _case(assertions=(
        {"id": "broken_trace", "text": "trace:no_such_check(1)", "weight": 1.0},
        {"id": "broken_code", "text": "code:output.missing_key", "weight": 1.0},
    ))
    signals = {s.evaluator_id: s for s in grader(_spec(case, tmp_path), _result(tmp_path), _trace())}
    assert signals["broken_trace"].verdict == "evaluator_error"
    assert signals["broken_code"].verdict == "evaluator_error"
    assert "missing_key" in signals["broken_code"].evidence


def test_a_judge_failure_is_an_evaluator_error(tmp_path):
    def exploding(assertion_text, candidate_content):
        raise RuntimeError("upstream 529")

    grader = build_grader(judge_backend=exploding, mutation_enabled=False)
    case = _case(assertions=({"id": "explains", "text": _JUDGE, "weight": 1.0},))
    (signal,) = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    assert signal.verdict == "evaluator_error"
    assert "upstream 529" in signal.evidence


def test_without_a_judge_backend_judge_assertions_are_skipped(tmp_path):
    """The documented zero-spend path: visibly skipped, and excluded from the
    slot denominators rather than counted as a pass."""
    _write_output(tmp_path, {"tests_added": 9})
    grader = build_grader(judge_backend=None, mutation_enabled=False)
    case = _case(assertions=(
        {"id": "wrote_tests", "text": _CODE, "weight": 1.0},
        {"id": "explains", "text": _JUDGE, "weight": 1.0},
    ))
    signals = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    judge_signal = next(s for s in signals if s.evaluator_id == "explains")
    assert judge_signal.verdict == "skipped"

    composed = compose_case(signals, "skill_behaviour", pass_threshold=0.7)
    assert composed.judge_disabled
    assert JUDGE_SLOT not in composed.slot_scores
    assert composed.score == pytest.approx(1.0)


# --------------------------------------------------------------------------
# the code: expression sandbox
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "expression",
    [
        "code:().__class__.__mro__",
        "code:(lambda: 1)()",
        "code:[x for x in output.__dict__]",
        "code:open('/etc/passwd')",
        "code:__import__('os').listdir('.')",
        # str.format interprets its format string at runtime, so the attribute
        # walk never appears in the AST for the allow-list to reject.
        'code:"{0.__class__}".format(output) != ""',
        'code:"{0.__class__}".format_map(output) != ""',
        # f-strings reach __format__ on the value for the same reason.
        'code:f"{output}" != ""',
    ],
)
def test_code_assertions_cannot_reach_past_their_namespace(expression):
    with pytest.raises(acceptance.CodeAssertionError):
        acceptance.evaluate_code_assertion(expression, {"output": {"a": 1}})


def test_a_code_assertion_cannot_hang_the_gate():
    """`9**9**9` is three tokens that allocate for minutes. Exponentiation is
    the one allow-listed operator whose cost is unbounded in the size of its
    operands, so it is rejected at parse time rather than evaluated."""
    with pytest.raises(acceptance.CodeAssertionError, match="exponentiation"):
        acceptance.evaluate_code_assertion("code:9**9**9 > 0", {"output": {}})
    # The bounded arithmetic an assertion actually uses still works.
    assert acceptance.evaluate_code_assertion(
        "code:output.n * 2 + 1 == 9", {"output": {"n": 4}}
    ).passed


def test_code_assertion_reads_nested_output_by_attribute_and_item():
    ns = {"output": {"pytest": {"failed": 0, "passed": 12}}}
    assert acceptance.evaluate_code_assertion(
        "code:output.pytest.failed == 0 and output['pytest']['passed'] > 10", ns
    ).passed


# --------------------------------------------------------------------------
# an evaluator that could not run is never scored as a zero
# --------------------------------------------------------------------------
def test_an_evaluator_error_is_excluded_from_its_slot_not_averaged_as_zero():
    """The rule the grader states and the composer has to honour: a check that
    could not be evaluated observed nothing. Folding its 0.0 into the slot mean
    would report the tool breaking as the candidate scoring badly."""
    passing = ci.ScoredSignal("ok", "assertion", 1.0, 1.0, "pass", slot="task_outcome")
    broken = ci.ScoredSignal(
        "mutation_kill_rate", "assertion", 0.0, 1.0, "evaluator_error",
        slot="task_outcome",
    )

    alone = compose_case([passing], "skill_behaviour", pass_threshold=0.7)
    with_error = compose_case([passing, broken], "skill_behaviour", pass_threshold=0.7)

    assert with_error.score == alone.score == pytest.approx(1.0)
    assert with_error.slot_scores["task_outcome"] == pytest.approx(1.0)
    assert with_error.passed

    # It is not silently dropped either: the count is how an operator learns
    # the score covers less than the checks the case declared.
    assert alone.evaluator_errors == 0
    assert with_error.evaluator_errors == 1


def test_a_slot_of_only_evaluator_errors_leaves_the_denominator():
    """With nothing evaluated in a slot, that slot contributes neither a score
    nor a weight, rather than dragging the case toward zero."""
    broken = ci.ScoredSignal(
        "mutation_kill_rate", "assertion", 0.0, 1.0, "evaluator_error",
        slot="task_outcome",
    )
    activation = ci.ScoredSignal(
        "used", "assertion", 1.0, 1.0, "pass", slot="activation_correct"
    )
    composed = compose_case([activation, broken], "skill_behaviour", pass_threshold=0.7)
    assert "task_outcome" not in composed.slot_scores
    assert composed.score == pytest.approx(1.0)
    assert composed.evaluator_errors == 1


def test_the_grader_emits_an_evaluator_error_the_composer_then_ignores(tmp_path):
    """End to end over the real grader: a malformed code: assertion must not
    pull its case down."""
    _write_output(tmp_path, {"tests_added": 4})
    grader = build_grader(mutation_enabled=False)
    case = _case(assertions=(
        {"id": "wrote_tests", "text": _CODE, "weight": 1.0},
        {"id": "broken", "text": "code:output.no_such_key > 1", "weight": 1.0},
    ))
    signals = grader(_spec(case, tmp_path), _result(tmp_path), _trace())
    composed = compose_case(signals, "skill_behaviour", pass_threshold=0.7)
    assert composed.evaluator_errors == 1
    assert composed.slot_scores["task_outcome"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# the judge is the only part of matrix/bench that bills, so it is opt-in
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "flag,env,expected",
    [
        (None, {}, False),                       # the default: no spend
        (None, {"ANTHROPIC_API_KEY": "sk-x"}, False),  # a key alone must not enable it
        (None, {"WOLOLO_EVALS_JUDGE": "on"}, True),
        ("on", {}, True),
        ("off", {"WOLOLO_EVALS_JUDGE": "on"}, False),  # the flag beats the environment
    ],
)
def test_the_judge_is_off_unless_asked(flag, env, expected, monkeypatch):
    from wololo_evals.cli.main import _judge_enabled, build_parser

    for name in ("WOLOLO_EVALS_JUDGE", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    argv = ["matrix", "--config", "x"] + (["--judge", flag] if flag else [])
    assert _judge_enabled(build_parser().parse_args(argv)) is expected


def test_enabling_the_judge_without_a_key_refuses_rather_than_scoring_skipped(monkeypatch):
    """Quietly emitting skipped signals here would look like a cheap pass on
    exactly the run an operator asked to be judged."""
    from wololo_evals.cli.main import _judge_backend, build_parser

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("WOLOLO_EVALS_JUDGE", "on")
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        _judge_backend(build_parser().parse_args(["matrix", "--config", "x"]))


def test_the_default_path_builds_no_judge_backend(monkeypatch):
    from wololo_evals.cli.main import _judge_backend, build_parser

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-used")
    monkeypatch.delenv("WOLOLO_EVALS_JUDGE", raising=False)
    assert _judge_backend(build_parser().parse_args(["matrix", "--config", "x"])) is None
