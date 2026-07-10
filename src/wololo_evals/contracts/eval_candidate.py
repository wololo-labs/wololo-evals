"""Builder for the ``eval-candidate`` contract family (04-wololo-evals SPEC 3.1).

The thing being evaluated by wololo-evals: a skill, prompt, harness config or
model config, produced by autoresearch (09), any other machine producer, or a
human via CLI. Validates against
``specs/eval-candidate/schemas/eval-candidate.schema.json``.

Pure, stdlib-only, mirroring ``core/contracts/learning.py``: required-field
``ValueError`` checks at construction so a malformed candidate cannot exist,
timestamps/ids default from the wall clock but may be supplied for
deterministic construction. The builder computes ``payload_sha256`` (sha256 of
the canonical-JSON payload): the content address used for dedupe, for the
blob-store key of derived artifacts, and for the integrity check after
decryption. The payload travels in the clear only on the TLS wire; the
wololo-evals persistence layer never stores it in plaintext.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Optional

from .timestamps import check_iso8601

__all__ = [
    "build_eval_candidate",
    "canonical_payload_sha256",
    "CANDIDATE_TYPES",
    "EVAL_SUITES",
]

CANDIDATE_TYPES = ("skill", "prompt", "harness-config", "model-config")
EVAL_SUITES = ("skill-review", "task-uplift", "repo-eval", "regression-replay")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_payload_sha256(payload: dict) -> str:
    """sha256 hex digest of the canonical-JSON encoding of *payload*.

    Canonical form: sorted keys, compact separators, no ASCII escaping of
    non-ASCII characters. Every consumer (dedupe, blob keys, post-decrypt
    integrity) must use this exact encoding.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_eval_candidate(
    tenant_id: str,
    candidate_type: str,
    candidate_ref: str,
    proposed_by: str,
    baseline_ref: str,
    payload: dict,
    *,
    id: Optional[str] = None,
    submitted_at: Optional[str] = None,
    rationale: Optional[str] = None,
    target_eval_suite: Optional[str] = None,
) -> dict:
    """Build a schema-valid ``eval-candidate`` record.

    Fail-closed: every required field missing or invalid raises ``ValueError``
    (an unknown ``candidate_type`` is never silently routed to a default
    suite; 04-wololo-evals SPEC 3.1).
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if candidate_type not in CANDIDATE_TYPES:
        raise ValueError(
            f"candidate_type {candidate_type!r} not in {CANDIDATE_TYPES}"
        )
    if not candidate_ref:
        raise ValueError("candidate_ref is required")
    if not proposed_by:
        raise ValueError("proposed_by is required")
    if not baseline_ref:
        raise ValueError("baseline_ref is required")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty dict")
    if target_eval_suite is not None and target_eval_suite not in EVAL_SUITES:
        raise ValueError(
            f"target_eval_suite {target_eval_suite!r} not in {EVAL_SUITES}"
        )

    record = {
        "schemaVersion": 1,
        "id": id or f"cand-{uuid.uuid4()}",
        "tenant_id": tenant_id,
        "candidate_type": candidate_type,
        "candidate_ref": candidate_ref,
        "proposed_by": proposed_by,
        "baseline_ref": baseline_ref,
        "payload": payload,
        "payload_sha256": canonical_payload_sha256(payload),
        "submitted_at": check_iso8601(
            submitted_at or _now_iso(), "submitted_at"
        ),
    }
    if rationale is not None:
        record["rationale"] = rationale
    if target_eval_suite is not None:
        record["target_eval_suite"] = target_eval_suite
    return record
