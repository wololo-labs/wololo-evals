"""The two mechanical QE scorers, run against the committed quoting-service
fixture, the same one the QE benchmark grades, so these tests prove the
numbers the gate reports are computed, not recorded.

* the mutation scorer really mutates and really runs the tests (marked
  ``mutation``: it drives mutmut in a subprocess and takes seconds, not
  milliseconds);
* the ``qe-review`` rubric's two static dimensions are pure AST work and run
  everywhere;
* a stronger suite must score strictly higher on both, or neither number is
  measuring test quality.

No Postgres, no network, no model.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from wololo_evals.scoring import mutation, qe_rubric
from wololo_evals.suites.base import load_rubric, rubric_path
from wololo_evals.versioning import rubric_version

_REPO = pathlib.Path(__file__).resolve().parents[1]
_FIXTURE = _REPO / "examples" / "fixtures" / "quoting-service"
_TARGET = _FIXTURE / "quoting"
_SHIPPED_TESTS = _FIXTURE / "tests"

mutmut_required = pytest.mark.skipif(
    pytest.importorskip.__module__ is None, reason="unreachable"
)


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


@pytest.fixture()
def changed_lines() -> dict:
    return json.loads((_FIXTURE / "changed_lines.json").read_text())["changed_lines"]


@pytest.fixture()
def boundary_tests(tmp_path) -> pathlib.Path:
    """A suite that pins the boundary the shipped one leaves open: the seeded
    defect (``weight_grams <= 0`` is quoted rather than rejected)."""
    path = tmp_path / "generated"
    path.mkdir()
    (path / "test_weight_boundary.py").write_text(
        '''
import json

import pytest

from quoting.pricing import (
    MAX_WEIGHT_GRAMS,
    QuoteRequest,
    QuoteRequestError,
    parse_quote_request,
    quote_pence,
)
from quoting.service import handle_quote


def _quote(**payload):
    return handle_quote(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("weight", [-1000, -1, 0])
def test_non_positive_weight_is_rejected(weight):
    status, body = _quote(weight_grams=weight, destination="uk")
    assert status == 400
    assert body["field"] == "weight_grams"


def test_one_gram_is_the_smallest_priced_parcel():
    status, body = _quote(weight_grams=1, destination="uk")
    assert status == 200
    assert body["quote_pence"] == 515


def test_weight_ceiling_is_inclusive():
    assert quote_pence(
        QuoteRequest(weight_grams=MAX_WEIGHT_GRAMS, destination="uk")
    ) == 395 + 120 * 30 + 850


def test_over_ceiling_is_rejected():
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request({"weight_grams": MAX_WEIGHT_GRAMS + 1, "destination": "uk"})
    assert excinfo.value.field == "weight_grams"


def test_boolean_weight_is_not_an_integer():
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request({"weight_grams": True, "destination": "uk"})
    assert excinfo.value.field == "weight_grams"
''',
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def vacuous_tests(tmp_path) -> pathlib.Path:
    """A suite that runs green and asserts nothing that can fail."""
    path = tmp_path / "vacuous"
    path.mkdir()
    (path / "test_vacuous.py").write_text(
        '''
from quoting.pricing import QuoteRequest, quote_pence


def test_it_runs():
    quote_pence(QuoteRequest(weight_grams=500, destination="uk"))
    assert True


def test_maths_still_works():
    assert 1 == 1


def test_a_price_comes_back():
    price = quote_pence(QuoteRequest(weight_grams=500, destination="uk"))
    assert price == price
''',
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# static dimensions
# --------------------------------------------------------------------------
def test_assertion_strength_sees_through_a_local_helper():
    """The shipped suite asserts on values it unpacked from a module-level
    helper, never on an imported name directly. Scoring only direct references
    would grade import style, not whether the expectation can fail."""
    score, details = qe_rubric.assertion_strength(_SHIPPED_TESTS, "quoting")
    assert details["test_functions"] == 7
    assert score > 0.8
    # The one weak test asserts a truthy `.startswith(...)`, pinning nothing.
    assert details["weak"] == ["test_quotes.py::test_health_endpoint_shape"]


def test_vacuous_assertions_are_caught(vacuous_tests):
    score, details = qe_rubric.no_tautological_assertions(vacuous_tests, "quoting")
    assert details["assertions"] == 3
    assert len(details["tautological"]) == 3  # assert True, 1 == 1, price == price
    assert score == 0.0

    strength, _ = qe_rubric.assertion_strength(vacuous_tests, "quoting")
    assert strength == 0.0


def test_the_shipped_suite_has_no_tautological_assertions():
    score, details = qe_rubric.no_tautological_assertions(_SHIPPED_TESTS, "quoting")
    assert details["tautological"] == []
    assert score == 1.0


# --------------------------------------------------------------------------
# dynamic dimensions + composite
# --------------------------------------------------------------------------
def test_rubric_composite_is_pinned_to_the_rubric_file(changed_lines):
    """The rubric's sha256 is its version: the composite must come from the
    weights in the file, not from a copy of them in the scorer."""
    report = qe_rubric.score_qe_rubric(_TARGET, [_SHIPPED_TESTS], changed_lines)
    assert set(report.dimensions) == {
        "assertion_strength", "flake_resistance",
        "changed_line_coverage", "no_tautological_assertions",
    }
    assert report.dimensions["flake_resistance"] == 1.0  # the fixture is stable

    rubric = load_rubric(qe_rubric.QE_SUITE)
    version = rubric_version(rubric_path(qe_rubric.QE_SUITE))
    signals = qe_rubric_signals = qe_rubric.qe_rubric_signals(
        report, rubric, threshold=0.7, rubric_version=version
    )
    composite = next(s for s in signals if s.evaluator_id == qe_rubric.QE_RUBRIC_EVALUATOR_ID)
    expected = sum(
        rubric["dimensions"][name]["weight"] * value
        for name, value in report.dimensions.items()
    )
    assert composite.score == pytest.approx(expected)
    assert composite.details["rubric_version"] == version
    assert len(version) == 64

    # Every dimension is also its own standing signal, so a gate report can
    # show which one moved without double-counting it against the composite.
    standing = [s for s in signals if s.slot is None]
    assert {s.evaluator_id for s in standing} == {
        f"qe_{name}" for name in report.dimensions
    }


def test_better_tests_cover_more_of_the_changed_lines(changed_lines, boundary_tests):
    shipped, shipped_details = qe_rubric.score_qe_rubric(
        _TARGET, [_SHIPPED_TESTS], changed_lines
    ), None
    combined = qe_rubric.score_qe_rubric(
        _TARGET, [_SHIPPED_TESTS, boundary_tests], changed_lines
    )
    assert (
        combined.dimensions["changed_line_coverage"]
        > shipped.dimensions["changed_line_coverage"]
    )
    assert combined.dimensions["flake_resistance"] == 1.0


# --------------------------------------------------------------------------
# mutation
# --------------------------------------------------------------------------
@needs_mutmut
@pytest.mark.mutation
def test_the_shipped_suite_leaves_mutants_alive():
    report = mutation.run_mutation_analysis(_TARGET, [_SHIPPED_TESTS])
    assert report.total > 50, "the fixture should yield a meaningful mutant population"
    assert report.killed > 0
    assert report.survived + report.never_reached > 0
    assert 0.0 < report.kill_rate < 1.0
    # Unreached mutants are survivals, so the strict rate is the lower one.
    assert report.kill_rate < report.kill_rate_executed


@needs_mutmut
@pytest.mark.mutation
def test_adding_boundary_tests_raises_the_kill_rate(boundary_tests):
    """The number the QE gate reports has to move when the tests get better,
    or it is not measuring anything."""
    before = mutation.run_mutation_analysis(_TARGET, [_SHIPPED_TESTS])
    after = mutation.run_mutation_analysis(_TARGET, [_SHIPPED_TESTS, boundary_tests])
    assert after.killed > before.killed
    assert after.kill_rate > before.kill_rate


@needs_mutmut
@pytest.mark.mutation
def test_a_vacuous_suite_kills_almost_nothing(vacuous_tests):
    """Green does not mean good: a suite whose assertions cannot fail runs
    clean and kills almost no mutants."""
    report = mutation.run_mutation_analysis(_TARGET, [vacuous_tests])
    assert report.kill_rate < 0.2


@needs_mutmut
@pytest.mark.mutation
def test_mutation_signal_carries_the_rate_and_a_pass_fail_verdict():
    report = mutation.run_mutation_analysis(_TARGET, [_SHIPPED_TESTS])
    signal = mutation.mutation_signal(report, threshold=0.9)
    assert signal.evaluator_id == mutation.MUTATION_EVALUATOR_ID
    assert signal.slot == "task_outcome"
    assert signal.score == pytest.approx(report.kill_rate)
    assert signal.verdict == "fail"  # 0.9 is above the shipped suite's rate
    assert mutation.mutation_signal(report, threshold=0.1).verdict == "pass"


def test_a_missing_mutation_tool_is_an_evaluator_error_not_a_zero():
    signal = mutation.error_signal("mutmut is not installed")
    assert signal.verdict == "evaluator_error"
    assert signal.score == 0.0


def test_an_empty_denominator_is_not_a_perfect_score():
    assert mutation.MutationReport(killed=0, survived=0).kill_rate == 0.0


def test_the_workspace_digest_is_content_addressed(tmp_path):
    """The grader caches the analysis on this, so two suites that differ must
    never collide and two identical ones must."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "test_x.py").write_text("assert True\n", encoding="utf-8")
    assert mutation.workspace_digest(a) == mutation.workspace_digest(b)
    (b / "test_x.py").write_text("assert False\n", encoding="utf-8")
    assert mutation.workspace_digest(a) != mutation.workspace_digest(b)
