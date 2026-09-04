"""Scoring pillar (open, Apache-2.0): turn a finished run into ``ScoredSignal``s.

* :mod:`~wololo_evals.scoring.grader` - the production grader ``matrix``/
  ``bench`` inject, dispatching each case assertion to the engine that can
  settle it (``trace:`` to the trajectory engine, ``code:`` to the acceptance
  runner, plain text to the hardened judge).
* :mod:`~wololo_evals.scoring.acceptance` - the ``code:`` expression engine.
* :mod:`~wololo_evals.scoring.mutation` - the mutation kill-rate scorer.
* :mod:`~wololo_evals.scoring.defects` - the seeded-defect catch-rate scorer.
* :mod:`~wololo_evals.scoring.qe_rubric` - the rule-based ``qe-review`` rubric.

Imports ``core.interfaces``, ``trace``, ``suites`` and stdlib only: no
database, no API, no vendor SDK. The judge is an injected callable.
"""
from __future__ import annotations

from wololo_evals.scoring.acceptance import (
    CodeAssertionError,
    CodeCheckResult,
    evaluate_code_assertion,
    is_code_assertion,
)
from wololo_evals.scoring.grader import (
    ACTIVATION_SLOT,
    JUDGE_SLOT,
    JudgeBackend,
    ProductionGrader,
    assertion_kind,
    build_grader,
)
from wololo_evals.scoring.defects import (
    DEFECT_EVALUATOR_ID,
    DefectApplyError,
    DefectError,
    DefectReport,
    SeededDefect,
    defect_signal,
    load_defects,
    run_defect_detection,
)
from wololo_evals.scoring.mutation import (
    MUTATION_EVALUATOR_ID,
    MutationError,
    MutationReport,
    MutationToolMissing,
    mutation_signal,
    run_mutation_analysis,
)
from wololo_evals.scoring.qe_rubric import (
    QE_RUBRIC_EVALUATOR_ID,
    QE_SUITE,
    QeRubricError,
    QeRubricReport,
    qe_rubric_signals,
    score_qe_rubric,
)

__all__ = [
    # grader
    "ProductionGrader",
    "build_grader",
    "assertion_kind",
    "JudgeBackend",
    "ACTIVATION_SLOT",
    "JUDGE_SLOT",
    # acceptance
    "evaluate_code_assertion",
    "is_code_assertion",
    "CodeAssertionError",
    "CodeCheckResult",
    # mutation
    "run_mutation_analysis",
    "mutation_signal",
    "MutationReport",
    "MutationError",
    "MutationToolMissing",
    "MUTATION_EVALUATOR_ID",
    # seeded defects
    "run_defect_detection",
    "load_defects",
    "defect_signal",
    "DefectReport",
    "SeededDefect",
    "DefectError",
    "DefectApplyError",
    "DEFECT_EVALUATOR_ID",
    # qe rubric
    "score_qe_rubric",
    "qe_rubric_signals",
    "QeRubricReport",
    "QeRubricError",
    "QE_SUITE",
    "QE_RUBRIC_EVALUATOR_ID",
]
