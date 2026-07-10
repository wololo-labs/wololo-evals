#!/usr/bin/env python3
"""Run the wololo-evals-quality-gate scenario (SPEC S14 rung 3).

Produces the three required proofs and writes
``meridian/proof/wololo-evals-quality-gate-<timestamp>.json``:

  1. baseline-vs-with-skill uplift on the KYC silent-drop task (5 paired trials
     per arm, mean uplift_pct, 95% CI, wins);
  2. repo-eval over real seeded history (correctness / churn_penalty /
     abstraction_adherence, three non-trivial dimensions);
  3. a deliberately regressing candidate driven through the canary lifecycle
     (watching -> regressed -> rolled-back, two audit rows, one idempotent
     rollback call).

Modes
-----
Degraded (default here, no docker daemon): proofs #1 and #2 run through the
harness-under-eval bridge's injected exec seam (deterministic fake harness,
real statistics) and proof #3 runs fully live (Postgres + deploy stub). The
generator is the pytest module ``tests/test_meridian_quality_gate.py``::

    WOLOLO_EVALS_WRITE_PROOF=1 pytest tests/test_meridian_quality_gate.py

Rung 3, real Atlas (OPERATOR PRECONDITION): with the Meridian fixture stood up
(the atlas-kyc-svc + atlas-onboarding-api seeded git repos), per-harness docker
sandbox images, and a model API key, the SAME proofs run against real harness
executions. Bring the stack up and re-run the generator inside it::

    docker compose up -d --wait          # in services/wololo-evals/
    WOLOLO_EVALS_COMPOSE_TRIAL=1 ANTHROPIC_API_KEY=... \
        WOLOLO_EVALS_WRITE_PROOF=1 pytest tests/test_meridian_quality_gate.py

This entrypoint reports the freshest committed proof and its pass/fail per rung.
"""
from __future__ import annotations

import json
import pathlib
import sys

PROOF_DIR = pathlib.Path(__file__).resolve().parents[2] / "proof"


def _latest_proof() -> pathlib.Path | None:
    proofs = sorted(PROOF_DIR.glob("wololo-evals-quality-gate-*.json"))
    return proofs[-1] if proofs else None


def _check(proofs: list[dict]) -> bool:
    by_id = {p["id"]: p for p in proofs}
    ok = True
    p1 = by_id.get("1-baseline-vs-skill-uplift", {})
    c1 = (len(p1.get("per_trial_composites", {}).get("baseline", [])) == 5
          and len(p1.get("per_trial_composites", {}).get("treatment", [])) == 5
          and p1.get("mean_uplift_pct", 0) > 0 and p1.get("wins") == 5)
    print(f"  [{'ok' if c1 else 'XX'}] proof 1 uplift: mean={p1.get('mean_uplift_pct')}%, "
          f"wins={p1.get('wins')}/{p1.get('n')}")
    ok = ok and c1

    p2 = by_id.get("2-repo-eval-real-history", {})
    c2 = p2.get("all_non_zero") and p2.get("all_distinct")
    print(f"  [{'ok' if c2 else 'XX'}] proof 2 repo-eval: {p2.get('dimensions')}")
    ok = ok and c2

    p3 = by_id.get("3-canary-regression-rollback", {})
    c3 = (p3.get("gate_status_transitions") == ["watching", "watching", "regressed", "rolled-back"]
          and p3.get("rollback_calls") == 1
          and p3.get("audit_actions") == ["canary-opened", "canary-regression-detected",
                                          "canary-rollback-fired"])
    print(f"  [{'ok' if c3 else 'XX'}] proof 3 canary: {p3.get('gate_status_transitions')}, "
          f"rollback_calls={p3.get('rollback_calls')}")
    return ok and c3


def main() -> int:
    proof = _latest_proof()
    if proof is None:
        print("wololo-evals-quality-gate: NO PROOF yet. Generate it with:")
        print("  WOLOLO_EVALS_WRITE_PROOF=1 pytest tests/test_meridian_quality_gate.py")
        return 1
    doc = json.loads(proof.read_text())
    print(f"wololo-evals-quality-gate: {proof.name}")
    print(f"  mode: {doc.get('mode')}")
    passed = _check(doc.get("proofs", []))
    print(f"wololo-evals-quality-gate: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
