"""DB-free eval core: the storage-agnostic library both frontends consume.

The file-first CLI (open, ``cli/``) and the Postgres/RLS service (closed,
``jobs/``, ``api.py``) both build on this package. Nothing here may import
``db.py``, ``api.py``, ``asgi.py``, ``audit.py``, or anything under ``jobs/``
— ``core`` must import cleanly with no database, no env vars, and no network.

``core.interfaces`` holds the binding cross-builder contracts (Protocols,
frozen records, shared constants, artefact-path helpers) that every wave-2
component codes against. This ``__init__`` is the *facade*: it re-exports the
pure wave-1 surface (suites, judge, stats, versioning, harness_solver,
scoring) under one clean namespace so a caller writes ``from wololo_evals.core
import run_judge, build_eval_score, verdict_stats_floored`` without reaching
into the service package's flat module layout. The underlying modules are not
moved — they are already library-shaped and DB-free — so both frontends share
one source of truth. See ``docs/plans/wave-contracts.md``.
"""
from __future__ import annotations

from . import interfaces, stats as stats_facade

# --- Judge (injection-hardened LLM judge; call_model is injected) ----------
from ..judge import (
    JUDGE_OUTPUT_SCHEMA,
    JudgeIntegrityError,
    JudgeParseError,
    JudgeResult,
    build_judge_messages,
    cli_call_model,
    http_call_model,
    parse_judge_output,
    prescan_flags,
    run_judge,
)

# --- Stats (raw wave-1 engine + in-library N>=5 floor gate) ----------------
from .stats import (
    N_TRIALS_FLOOR,
    FloorViolation,
    check_trial_floor,
    paired_uplift_pcts,
    single_arm_stats,
    single_arm_stats_floored,
    t_critical_95,
    task_uplift_passed,
    verdict_stats,
    verdict_stats_floored,
)

# --- Versioning (rubric/suite content hashing; archive takes a blobstore) --
from ..versioning import archive_rubric, rubric_version, suite_version

# --- Harness solver (subprocess bridge over an injected SandboxExec) --------
from ..harness_solver import (
    HarnessTrialConfig,
    HarnessTrialResult,
    SandboxExec,
    TrialErrored,
    count_turns,
    materialize_oracle_files,
    run_acceptance_test,
    run_harness_trial,
)

# --- Suites (rubric loader + composite/efficiency scoring helpers) ----------
from ..suites.base import (
    SUITE_DIRS,
    composite,
    judged_dimensions,
    load_rubric,
    rubric_judge_text,
    rubric_path,
    suite_dir,
    turns_efficiency_score,
)

# --- Scoring / result contracts (build_eval_score & friends) ----------------
# NOTE: contracts.eval_result re-uses the suite enum via a mid-file
# ``from .eval_candidate import EVAL_SUITES`` (eval_result.py:51). EVAL_SUITES'
# source of truth is eval_candidate; re-export it from there so the facade
# never depends on that non-standard import placement.
from ..contracts.eval_candidate import EVAL_SUITES
from ..contracts.eval_result import (
    RUN_STATUSES,
    TRIAL_ARMS,
    TRIAL_STATUSES,
    VERDICT_STAT_KEYS,
    build_eval_run,
    build_eval_score,
    build_eval_trial,
    build_leaderboard_entry,
)

__all__ = [
    "interfaces",
    "stats_facade",
    # judge
    "run_judge",
    "JudgeResult",
    "JudgeIntegrityError",
    "JudgeParseError",
    "JUDGE_OUTPUT_SCHEMA",
    "build_judge_messages",
    "parse_judge_output",
    "prescan_flags",
    "http_call_model",
    "cli_call_model",
    # stats + floor
    "verdict_stats",
    "single_arm_stats",
    "paired_uplift_pcts",
    "task_uplift_passed",
    "t_critical_95",
    "N_TRIALS_FLOOR",
    "FloorViolation",
    "check_trial_floor",
    "verdict_stats_floored",
    "single_arm_stats_floored",
    # versioning
    "rubric_version",
    "suite_version",
    "archive_rubric",
    # harness solver
    "run_harness_trial",
    "run_acceptance_test",
    "materialize_oracle_files",
    "count_turns",
    "HarnessTrialConfig",
    "HarnessTrialResult",
    "TrialErrored",
    "SandboxExec",
    # suites
    "suite_dir",
    "rubric_path",
    "load_rubric",
    "judged_dimensions",
    "rubric_judge_text",
    "composite",
    "turns_efficiency_score",
    "SUITE_DIRS",
    # scoring / result contracts
    "build_eval_run",
    "build_eval_trial",
    "build_eval_score",
    "build_leaderboard_entry",
    "EVAL_SUITES",
    "RUN_STATUSES",
    "TRIAL_STATUSES",
    "TRIAL_ARMS",
    "VERDICT_STAT_KEYS",
]
