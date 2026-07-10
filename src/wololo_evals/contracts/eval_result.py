"""Builders for the ``eval-result`` contract family (04-wololo-evals SPEC 3.2).

Four kinds, one per granularity: ``eval-run`` (aggregate over N trials),
``eval-trial`` (per-trial raw result; first-class so n=1 verdicts are
structurally impossible to hide, DOCTRINE 15.6), ``eval-score`` (one scored
dimension, additive, never overwritten) and ``leaderboard-entry`` (one
(model, harness, skill) ranking row). Schemas at
``specs/eval-result/schemas/``.

Pure, stdlib-only, mirroring ``core/contracts/learning.py``. Version pinning
(SPEC Section 9) is structural here: ``suite_version`` and ``rubric_version``
are required 64-hex content hashes on every run, score and leaderboard row so
an unpinned row cannot be built.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from .timestamps import check_iso8601

__all__ = [
    "build_eval_run",
    "build_eval_trial",
    "build_eval_score",
    "build_leaderboard_entry",
    "RUN_STATUSES",
    "TRIAL_STATUSES",
    "TRIAL_ARMS",
    "VERDICT_STAT_KEYS",
]

RUN_STATUSES = ("queued", "running", "passed", "failed", "errored")
TRIAL_STATUSES = ("running", "completed", "errored")
TRIAL_ARMS = ("baseline", "treatment", "single")
VERDICT_STAT_KEYS = (
    "mean",
    "stddev",
    "ci_low",
    "ci_high",
    "wins",
    "losses",
    "n",
    "low_confidence",
)

_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# Reuse the suite enum from the sibling family; one source of truth.
from .eval_candidate import EVAL_SUITES  # noqa: E402


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_content_hash(value: str, field: str) -> str:
    if not _CONTENT_HASH_RE.match(value or ""):
        raise ValueError(
            f"{field} {value!r} must be a 64-char lowercase sha256 hex digest"
        )
    return value


def _check_verdict_stats(stats: dict) -> dict:
    if set(stats) != set(VERDICT_STAT_KEYS):
        raise ValueError(
            f"verdict_stats keys {sorted(stats)} != required {sorted(VERDICT_STAT_KEYS)}"
        )
    if not isinstance(stats["low_confidence"], bool):
        raise ValueError("verdict_stats.low_confidence must be a bool")
    for key in ("wins", "losses", "n"):
        if not isinstance(stats[key], int) or stats[key] < 0:
            raise ValueError(f"verdict_stats.{key} must be a non-negative int")
    if stats["n"] < 1:
        raise ValueError("verdict_stats.n must be >= 1")
    return stats


def build_eval_run(
    tenant_id: str,
    run_id: str,
    candidate_id: str,
    eval_suite: str,
    status: str,
    suite_version: str,
    rubric_version: str,
    n_trials: int,
    *,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    composite_score: Optional[float] = None,
    verdict_stats: Optional[dict] = None,
    eval_log_prefix: Optional[str] = None,
) -> dict:
    """Build a schema-valid ``eval-run`` record."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not run_id:
        raise ValueError("run_id is required")
    if not candidate_id:
        raise ValueError("candidate_id is required")
    if eval_suite not in EVAL_SUITES:
        raise ValueError(f"eval_suite {eval_suite!r} not in {EVAL_SUITES}")
    if status not in RUN_STATUSES:
        raise ValueError(f"status {status!r} not in {RUN_STATUSES}")
    _check_content_hash(suite_version, "suite_version")
    _check_content_hash(rubric_version, "rubric_version")
    if not isinstance(n_trials, int) or n_trials < 1:
        raise ValueError(f"n_trials must be a positive int, got {n_trials!r}")

    record = {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "eval_suite": eval_suite,
        "status": status,
        "suite_version": suite_version,
        "rubric_version": rubric_version,
        "n_trials": n_trials,
    }
    if started_at is not None:
        record["started_at"] = check_iso8601(started_at, "started_at")
    if completed_at is not None:
        record["completed_at"] = check_iso8601(completed_at, "completed_at")
    if composite_score is not None:
        if composite_score < 0:
            raise ValueError("composite_score must be >= 0")
        record["composite_score"] = float(composite_score)
    if verdict_stats is not None:
        record["verdict_stats"] = _check_verdict_stats(dict(verdict_stats))
    if eval_log_prefix is not None:
        if not eval_log_prefix:
            raise ValueError("eval_log_prefix must be non-empty when supplied")
        record["eval_log_prefix"] = eval_log_prefix
    return record


def build_eval_trial(
    tenant_id: str,
    run_id: str,
    trial_index: int,
    arm: str,
    status: str,
    *,
    model: Optional[str] = None,
    harness: Optional[str] = None,
    composite: Optional[float] = None,
    eval_log_path: Optional[str] = None,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> dict:
    """Build a schema-valid ``eval-trial`` record."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not run_id:
        raise ValueError("run_id is required")
    if not isinstance(trial_index, int) or trial_index < 0:
        raise ValueError(f"trial_index must be a non-negative int, got {trial_index!r}")
    if arm not in TRIAL_ARMS:
        raise ValueError(f"arm {arm!r} not in {TRIAL_ARMS}")
    if status not in TRIAL_STATUSES:
        raise ValueError(f"status {status!r} not in {TRIAL_STATUSES}")

    record = {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "trial_index": trial_index,
        "arm": arm,
        "status": status,
    }
    if model is not None:
        record["model"] = model
    if harness is not None:
        record["harness"] = harness
    if composite is not None:
        record["composite"] = float(composite)
    if eval_log_path is not None:
        record["eval_log_path"] = eval_log_path
    if started_at is not None:
        record["started_at"] = check_iso8601(started_at, "started_at")
    if completed_at is not None:
        record["completed_at"] = check_iso8601(completed_at, "completed_at")
    return record


def build_eval_score(
    tenant_id: str,
    run_id: str,
    dimension: str,
    value: float,
    suite_version: str,
    rubric_version: str,
    *,
    trial_index: Optional[int] = None,
    weight: float = 1.0,
    max_value: float = 1.0,
    judge: Optional[str] = None,
    rationale: Optional[str] = None,
) -> dict:
    """Build a schema-valid ``eval-score`` record.

    ``trial_index=None`` marks the aggregate row over all trials; an integer
    marks a per-trial raw sample (including the k=5 raw judge samples).
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not run_id:
        raise ValueError("run_id is required")
    if not dimension:
        raise ValueError("dimension is required")
    _check_content_hash(suite_version, "suite_version")
    _check_content_hash(rubric_version, "rubric_version")
    if trial_index is not None and (
        not isinstance(trial_index, int) or trial_index < 0
    ):
        raise ValueError(f"trial_index must be a non-negative int, got {trial_index!r}")
    if weight <= 0:
        raise ValueError("weight must be > 0")
    if max_value <= 0:
        raise ValueError("max_value must be > 0")

    record = {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "dimension": dimension,
        "value": float(value),
        "suite_version": suite_version,
        "rubric_version": rubric_version,
        "trial_index": trial_index,
        "weight": float(weight),
        "max_value": float(max_value),
    }
    if judge is not None:
        record["judge"] = judge
    if rationale is not None:
        record["rationale"] = rationale
    return record


def build_leaderboard_entry(
    tenant_id: str,
    model: str,
    harness: str,
    skill_ref: str,
    composite_score: float,
    suite_version: str,
    rubric_version: str,
    *,
    run_id: str,
    n_trials: int = 1,
    score_stddev: Optional[float] = None,
    ci_low: Optional[float] = None,
    ci_high: Optional[float] = None,
    computed_at: Optional[str] = None,
) -> dict:
    """Build a schema-valid ``leaderboard-entry`` record.

    ``composite_score`` must be the referenced run's frozen
    ``runs.composite_score``; the leaderboard is a rebuildable read-model over
    runs, never a second source of truth (SPEC 5.4).
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not model:
        raise ValueError("model is required")
    if not harness:
        raise ValueError("harness is required")
    if not skill_ref:
        raise ValueError("skill_ref is required")
    _check_content_hash(suite_version, "suite_version")
    _check_content_hash(rubric_version, "rubric_version")
    if not run_id:
        raise ValueError("run_id is required")
    if not isinstance(n_trials, int) or n_trials < 1:
        raise ValueError(f"n_trials must be a positive int, got {n_trials!r}")
    if score_stddev is not None and score_stddev < 0:
        raise ValueError("score_stddev must be >= 0")

    record = {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "model": model,
        "harness": harness,
        "skill_ref": skill_ref,
        "composite_score": float(composite_score),
        "suite_version": suite_version,
        "rubric_version": rubric_version,
        "run_id": run_id,
        "n_trials": n_trials,
    }
    if score_stddev is not None:
        record["score_stddev"] = float(score_stddev)
    if ci_low is not None:
        record["ci_low"] = float(ci_low)
    if ci_high is not None:
        record["ci_high"] = float(ci_high)
    record["computed_at"] = check_iso8601(computed_at or _now_iso(), "computed_at")
    return record
