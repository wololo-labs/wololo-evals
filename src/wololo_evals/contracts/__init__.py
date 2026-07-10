"""Vendored kernel contract families (canonical home: fleet-lambda core/contracts).

Vendor pin: CONTRACT_VERSION 1.0; fleet-lambda origin/main 4ba028f plus branch
f/eval-contracts 45bab59 (PR #57, the eval families' canonical landing). Re-pin
on upstream MAJOR bump; ledger in docs/INTEGRATION-DEBT.md. Never edited here
beyond the identity schema-dir repoint; fixes go upstream via BRANCH+PR.
"""
from .eval_candidate import (
    CANDIDATE_TYPES,
    EVAL_SUITES,
    build_eval_candidate,
    canonical_payload_sha256,
)
from .eval_result import (
    RUN_STATUSES,
    TRIAL_ARMS,
    TRIAL_STATUSES,
    VERDICT_STAT_KEYS,
    build_eval_run,
    build_eval_score,
    build_eval_trial,
    build_leaderboard_entry,
)
from .identity import (
    PLANS,
    build_entitlement_claims,
    is_expired,
    validate_entitlement_token,
)
from .timestamps import ISO8601_PATTERN, check_iso8601

__all__ = [
    "CANDIDATE_TYPES",
    "EVAL_SUITES",
    "build_eval_candidate",
    "canonical_payload_sha256",
    "RUN_STATUSES",
    "TRIAL_ARMS",
    "TRIAL_STATUSES",
    "VERDICT_STAT_KEYS",
    "build_eval_run",
    "build_eval_score",
    "build_eval_trial",
    "build_leaderboard_entry",
    "PLANS",
    "build_entitlement_claims",
    "is_expired",
    "validate_entitlement_token",
    "ISO8601_PATTERN",
    "check_iso8601",
]
