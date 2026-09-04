"""The production grader (open, Apache-2.0).

This is the seam ``matrix``/``bench`` call once per (case, seed) to turn a
finished run into ``ScoredSignal``s. Every assertion a case declares is
dispatched by its kind, and each kind goes to the engine that can actually
settle it:

============================  =============================================
``trace:check(...)``          ``trace.evaluate_trace_assertion`` over the
                              normalised ``TraceEvent`` sequence
``code:<expression>``         ``scoring.acceptance`` over the run's recorded
                              outputs
anything else (plain text)    the hardened LLM judge
============================  =============================================

On top of the per-assertion signals the grader runs the *standing* QE scorers
for any case that declares a ``qe`` block in its metadata: the mutation kill
rate, the seeded-defect catch rate, and the four ``qe-review`` rubric
dimensions, all computed from the candidate's own generated tests. Those are
mechanical, run offline, and cost nothing, so they are produced at gate time
rather than read back from a recording.

Failure discipline, in one rule: **a check that could not be evaluated is
never a zero.** A malformed assertion, a missing recorded output, an
unavailable mutation tool - each becomes an ``evaluator_error`` signal that
stays visible in the scorecard. A zero has to mean "the run did not satisfy
this", or the whole gate is unreadable.

The judge is injected (``judge_backend``), never constructed here: without one
the judge signals are emitted with verdict ``skipped``, which ``compose_case``
already excludes from the slot denominators. That is the documented
zero-spend path, and it is a visible ``skipped``, not an invisible pass.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from wololo_evals.core import interfaces as ci
from wololo_evals.scoring import acceptance, defects, mutation, qe_rubric
from wololo_evals.trace import assertions as trace_assertions

__all__ = [
    "ACTIVATION_SLOT",
    "JUDGE_SLOT",
    "JudgeBackend",
    "ProductionGrader",
    "build_grader",
    "assertion_kind",
]

#: §16.1 slot a ``trace:used_skill`` assertion feeds - activation is its own
#: slot in every family that has one, distinct from trace conformance.
ACTIVATION_SLOT = "activation_correct"

#: §16.1 slot every plain-text (judge) assertion feeds.
JUDGE_SLOT = "llm_judge_overall"

#: ``(assertion_text, candidate_content) -> (score in [0, 1], evidence)``.
JudgeBackend = Callable[[str, str], tuple[float, str]]

#: Recorded-output filename the ``code:`` namespace's ``output`` comes from.
OUTPUT_FILENAME = "output.json"

#: Case-metadata key declaring the QE target. See the QE benchmark's
#: ``cases.toml`` for the authored shape.
QE_METADATA_KEY = "qe"


def assertion_kind(text: str) -> str:
    """``"trace"``, ``"code"``, or ``"judge"`` for one assertion body."""
    if trace_assertions.is_trace_assertion(text):
        return "trace"
    if acceptance.is_code_assertion(text):
        return "code"
    return "judge"


def _evaluator_error(
    evaluator_id: str, slot: Optional[str], weight: float, reason: str
) -> ci.ScoredSignal:
    return ci.ScoredSignal(
        evaluator_id=evaluator_id,
        kind="assertion",
        score=0.0,
        weight=weight,
        verdict="evaluator_error",
        slot=slot,
        details={"error": reason},
        evidence=reason,
    )


def _trace_slot(check: str) -> str:
    return ACTIVATION_SLOT if check == "used_skill" else trace_assertions.TRACE_CONFORMANCE_SLOT


def _advisory_dir(spec: ci.RunSpec, result: ci.RunResult) -> pathlib.Path:
    """Where this run's capture files landed.

    ``RunResult.artefact_dir`` is authoritative (adapters set it); the spec's
    workspace is the fallback for an adapter that reported a bare directory.
    """
    if result.artefact_dir:
        return pathlib.Path(result.artefact_dir)
    return pathlib.Path(spec.cwd) / ci.ADVISORY_DIR


def _read_output(spec: ci.RunSpec, result: ci.RunResult) -> Mapping[str, object]:
    """Load ``output.json`` from the run's capture directory.

    A run with no recorded output yields ``{}``: the ``code:`` assertion then
    fails loudly on its first missing key, which is the honest reading of
    "the run produced nothing to accept".
    """
    for candidate in (
        _advisory_dir(spec, result) / OUTPUT_FILENAME,
        pathlib.Path(spec.cwd) / ci.ADVISORY_DIR / OUTPUT_FILENAME,
        pathlib.Path(spec.cwd) / OUTPUT_FILENAME,
    ):
        if candidate.is_file():
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, Mapping) else {"value": loaded}
    return {}


def _case_namespace(
    spec: ci.RunSpec,
    result: ci.RunResult,
    trace: Sequence[ci.TraceEvent],
) -> dict:
    return {
        "output": _read_output(spec, result),
        "result": {
            "status": result.status,
            "cost_usd": result.cost_usd,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "attempts": result.attempts,
            "runtime_seconds": result.runtime_seconds,
        },
        "case": {
            "case_id": spec.case.case_id,
            "role": spec.case.role,
            "expected_outcome": spec.case.expected_outcome,
            "expected_skill": spec.case.expected_skill,
            "metadata": dict(spec.case.metadata),
        },
        "trace": [
            {"seq": e.seq, "kind": e.kind, "name": e.name, "target": e.target}
            for e in trace
        ],
    }


def _candidate_content(spec: ci.RunSpec, namespace: Mapping[str, object]) -> str:
    """What the judge is shown: the run's recorded answer, or (absent one) the
    run's output blob. Never the rubric, never the assertion - SPEC S8 keeps
    instructions and candidate data structurally apart."""
    output = namespace.get("output") or {}
    if isinstance(output, Mapping):
        for key in ("answer", "summary", "text", "message"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2)


@dataclass(frozen=True)
class ProductionGrader:
    """The default ``matrix.Grader``: dispatch assertions, run standing scorers.

    ``judge_backend`` is optional by design (see the module docstring);
    ``mutation_enabled`` turns the standing QE scorers off for a run that only
    needs the assertion engines, and ``benchmark_dir`` roots the relative paths
    a case's ``mutation`` metadata declares.
    """

    judge_backend: Optional[JudgeBackend] = None
    benchmark_dir: Optional[pathlib.Path] = None
    mutation_enabled: bool = True
    mutation_threshold: float = 0.7
    defect_threshold: float = 0.75
    qe_threshold: float = 0.7
    #: (target digest, tests digest) -> report, so N seeds of one case (and a
    #: whole arm sharing one test suite) run the analysis once.
    _cache: dict = field(default_factory=dict, compare=False, repr=False)

    # -- assertion dispatch ------------------------------------------------
    def _grade_assertion(
        self,
        row: Mapping[str, object],
        spec: ci.RunSpec,
        namespace: Mapping[str, object],
        trace: Sequence[ci.TraceEvent],
    ) -> ci.ScoredSignal:
        aid = str(row.get("id") or "assertion")
        text = str(row.get("text") or "")
        weight = float(row.get("weight", 1.0))
        kind = assertion_kind(text)

        if kind == "trace":
            try:
                check = trace_assertions.evaluate_trace_assertion(
                    text, trace, default_skill=spec.case.expected_skill
                )
            except trace_assertions.TraceAssertionError as exc:
                return _evaluator_error(
                    aid, trace_assertions.TRACE_CONFORMANCE_SLOT, weight,
                    f"trace assertion {text!r}: {exc}",
                )
            signal = check.to_signal(evaluator_id=aid, weight=weight)
            slot = _trace_slot(check.check)
            if slot != ACTIVATION_SLOT:
                return signal
            # `used_skill` feeds activation_correct, and on a distractor the
            # correct behaviour is *not* activating: score the inverse, or a
            # skill that fired where it should have stayed silent would take
            # full marks on the very slot that exists to catch it (§16.1).
            expect_activation = spec.case.expected_outcome != "no_activation"
            passed = check.passed if expect_activation else not check.passed
            evidence = check.evidence
            if not expect_activation:
                evidence = (
                    f"distractor: {evidence} "
                    f"({'correctly silent' if passed else 'activated anyway'})"
                )
            return ci.ScoredSignal(
                evaluator_id=signal.evaluator_id, kind=signal.kind,
                score=1.0 if passed else 0.0, weight=weight,
                verdict="pass" if passed else "fail", slot=ACTIVATION_SLOT,
                details=dict(check.details) | {"expect_activation": expect_activation},
                evidence=evidence,
            )

        if kind == "code":
            try:
                check = acceptance.evaluate_code_assertion(text, namespace)
            except acceptance.CodeAssertionError as exc:
                return _evaluator_error(
                    aid, acceptance.TASK_OUTCOME_SLOT, weight, str(exc)
                )
            return check.to_signal(evaluator_id=aid, weight=weight)

        if self.judge_backend is None:
            return ci.ScoredSignal(
                evaluator_id=aid, kind="judge", score=0.0, weight=weight,
                verdict="skipped", slot=JUDGE_SLOT,
                details={"reason": "no judge backend configured"},
                evidence="judge disabled: no backend configured for this run",
            )
        try:
            score, evidence = self.judge_backend(
                text, _candidate_content(spec, namespace)
            )
        except Exception as exc:  # noqa: BLE001 - a judge failure is an error
            return ci.ScoredSignal(
                evaluator_id=aid, kind="judge", score=0.0, weight=weight,
                verdict="evaluator_error", slot=JUDGE_SLOT,
                details={"error": f"{type(exc).__name__}: {exc}"},
                evidence=f"judge call failed: {type(exc).__name__}: {exc}",
            )
        return ci.ScoredSignal(
            evaluator_id=aid, kind="judge", score=float(score), weight=weight,
            verdict="pass" if float(score) >= 0.5 else "fail", slot=JUDGE_SLOT,
            details={"assertion": text}, evidence=evidence,
        )

    # -- standing QE scorers ----------------------------------------------
    def _resolve(self, rel: str, spec: ci.RunSpec) -> pathlib.Path:
        """Resolve a case-declared path: workspace-relative first (the run's
        own output), then benchmark-relative (a committed fixture)."""
        path = pathlib.Path(rel)
        if path.is_absolute():
            return path
        in_workspace = pathlib.Path(spec.cwd) / path
        if in_workspace.exists():
            return in_workspace
        if self.benchmark_dir is not None:
            return (self.benchmark_dir / path).resolve()
        return in_workspace

    def _changed_lines(
        self, config: Mapping[str, object], spec: ci.RunSpec
    ) -> dict[str, list[int]]:
        """The changed-line map, inline or from the fixture's manifest.

        A benchmark with a dozen cases over one fixture should declare the
        lines under review once, next to the code, not copy the same list into
        every case.
        """
        rel = config.get("changed_lines_file")
        if rel:
            manifest = json.loads(
                self._resolve(str(rel), spec).read_text(encoding="utf-8")
            )
            raw = manifest.get("changed_lines", manifest)
        else:
            raw = config.get("changed_lines", {})
        return {str(k): [int(n) for n in v] for k, v in dict(raw).items()}

    def _qe_signals(self, spec: ci.RunSpec) -> list[ci.ScoredSignal]:
        config = spec.case.metadata.get(QE_METADATA_KEY)
        if not self.mutation_enabled or not isinstance(config, Mapping):
            return [], {}
        if not config.get("enabled", True):
            # A case that shares the casebook's fixture block but has no test
            # authoring in it (a distractor) opts out explicitly, rather than
            # scoring the fixture's own suite as if the run had produced it.
            return [], {}

        target = self._resolve(str(config.get("target", "")), spec)
        tests = [self._resolve(str(p), spec) for p in config.get("tests", ())]
        changed_lines = self._changed_lines(config, spec)
        threshold = float(config.get("mutation_threshold", self.mutation_threshold))
        defect_threshold = float(config.get("defect_threshold", self.defect_threshold))
        defects_manifest = config.get("defects_file")
        defects_path = (
            self._resolve(str(defects_manifest), spec) if defects_manifest else None
        )

        key = (
            mutation.workspace_digest(target),
            mutation.workspace_digest(*tests),
            json.dumps(changed_lines, sort_keys=True),
            str(defects_path),
        )
        if key not in self._cache:
            self._cache[key] = self._compute_qe(
                target, tests, changed_lines, defects_path
            )
        mutation_report, rubric_report, defect_report, errors = self._cache[key]

        qe_view: dict[str, object] = {}
        if mutation_report is not None:
            qe_view["mutation"] = mutation_report.as_dict()
        if rubric_report is not None:
            qe_view["rubric"] = dict(rubric_report.dimensions)
        if defect_report is not None:
            qe_view["defects"] = defect_report.as_dict()

        signals: list[ci.ScoredSignal] = []
        if mutation_report is not None:
            signals.append(
                mutation.mutation_signal(mutation_report, threshold=threshold)
            )
        else:
            signals.append(mutation.error_signal(errors.get("mutation", "unknown")))

        if defects_path is not None:
            if defect_report is not None:
                signals.append(
                    defects.defect_signal(defect_report, threshold=defect_threshold)
                )
            else:
                signals.append(defects.error_signal(errors.get("defects", "unknown")))

        if rubric_report is not None:
            from wololo_evals.suites.base import load_rubric, rubric_path
            from wololo_evals.versioning import rubric_version

            signals.extend(
                qe_rubric.qe_rubric_signals(
                    rubric_report,
                    load_rubric(qe_rubric.QE_SUITE),
                    threshold=self.qe_threshold,
                    rubric_version=rubric_version(rubric_path(qe_rubric.QE_SUITE)),
                )
            )
        else:
            signals.extend(qe_rubric.error_signals(errors.get("qe_rubric", "unknown")))
        return signals, qe_view

    def _compute_qe(self, target, tests, changed_lines, defects_path):
        """Run every standing scorer once for one (target, tests) pair.

        Each is independent: one scorer failing must not hide the other two,
        so their errors are collected rather than raised.
        """
        errors: dict[str, str] = {}
        mutation_report = rubric_report = defect_report = None
        try:
            mutation_report = mutation.run_mutation_analysis(target, tests)
        except mutation.MutationError as exc:
            errors["mutation"] = str(exc)
        try:
            rubric_report = qe_rubric.score_qe_rubric(target, tests, changed_lines)
        except (qe_rubric.QeRubricError, OSError) as exc:
            errors["qe_rubric"] = str(exc)
        if defects_path is not None:
            try:
                defect_report = defects.run_defect_detection(
                    target, tests, defects.load_defects(defects_path)
                )
            except (defects.DefectError, OSError, subprocess.TimeoutExpired) as exc:
                errors["defects"] = str(exc)
        return mutation_report, rubric_report, defect_report, errors

    # -- the Grader seam ---------------------------------------------------
    def __call__(
        self,
        spec: ci.RunSpec,
        result: ci.RunResult,
        trace: Sequence[ci.TraceEvent],
    ) -> list[ci.ScoredSignal]:
        # The standing scorers run first so a ``code:`` assertion can accept on
        # a number this gate just computed (``code:qe.mutation.kill_rate >=
        # 0.6``) rather than on one the recording asserted about itself.
        qe_signals, qe_view = self._qe_signals(spec)
        namespace = _case_namespace(spec, result, trace)
        namespace["qe"] = qe_view
        signals = [
            self._grade_assertion(row, spec, namespace, trace)
            for row in spec.case.assertions
        ]
        signals.extend(qe_signals)
        return signals


def build_grader(
    *,
    judge_backend: Optional[JudgeBackend] = None,
    benchmark_dir: Optional[str | pathlib.Path] = None,
    mutation_enabled: bool = True,
) -> ProductionGrader:
    """Construct the default grader ``matrix``/``bench`` inject."""
    return ProductionGrader(
        judge_backend=judge_backend,
        benchmark_dir=pathlib.Path(benchmark_dir).resolve() if benchmark_dir else None,
        mutation_enabled=mutation_enabled,
    )
