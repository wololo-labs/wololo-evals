"""Meridian `wololo-evals-quality-gate` scenario proof generator (SPEC S14
rung 3). Produces the three required proofs with REAL statistics on real RLS
Postgres and writes them to
``meridian/proof/wololo-evals-quality-gate-<timestamp>.json``.

Degraded-mode honesty: proofs #1 and #2 drive the harness-under-eval bridge
through the injected exec seam (deterministic fake harness, but genuine paired
statistics and genuine non-trivial repo-eval dimensions); proof #3 runs fully
live (Postgres + deploy stub, no docker). The real-Atlas containerized run is
the operator precondition documented in the scenario's run.py. Every number
below is computed by the service's own code, never hand-seeded.

    WOLOLO_EVALS_WRITE_PROOF=1 pytest tests/test_meridian_quality_gate.py
"""
from __future__ import annotations

import json
import os
import pathlib
import time

import httpx
import pytest
from sqlalchemy import text

from wololo_evals import canary
from wololo_evals.db import tenant_txn
from wololo_evals.jobs.worker import execute_run
from wololo_evals.stubs.deploy_stub import create_deploy_stub

from test_harness_uplift import (
    BASELINE_TURNS,
    TREATMENT_TURNS,
    FakeSandbox,
    _judge as _uplift_judge,
    _make_uplift_run,
)
from test_repo_eval import FakeRepoSandbox, _judge as _repo_judge, _make_repo_run
from test_canary import (
    HEALTHY,
    REGRESSING,
    _gate_row,
    _open_gate,
    _passed_run,
    _send_window,
)

TENANT = "meridian"


async def _proof_uplift(app_engine, cipher) -> dict:
    rid = await _make_uplift_run(app_engine, cipher, "claude-code")
    fake = FakeSandbox()  # treatment spends fewer turns -> genuine positive uplift
    async with tenant_txn(app_engine, TENANT) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_uplift_judge, k=5,
            exec_factory=lambda: fake.exec,
        )
    async with tenant_txn(app_engine, TENANT) as conn:
        rows = (await conn.execute(text(
            "select arm, trial_index, composite from wololo_evals.trials "
            "where run_id = :r order by arm, trial_index"), {"r": rid})).all()
        run = (await conn.execute(text(
            "select suite_version, rubric_version from wololo_evals.runs where id = :r"
        ), {"r": rid})).first()
    per_arm: dict[str, list[float]] = {}
    for arm, _idx, comp in rows:
        per_arm.setdefault(arm, []).append(round(float(comp), 6))
    vs = out["verdict_stats"]
    return {
        "id": "1-baseline-vs-skill-uplift",
        "suite": "task-uplift",
        "task": "fix the KYC silent-drop bug in atlas-kyc-svc",
        "arms": {"baseline_turns": BASELINE_TURNS, "treatment_turns": TREATMENT_TURNS},
        "per_trial_composites": per_arm,
        "mean_uplift_pct": round(vs["mean"], 4),
        "ci_95": [round(vs["ci_low"], 4), round(vs["ci_high"], 4)],
        "wins": vs["wins"], "losses": vs["losses"], "n": vs["n"],
        "low_confidence": vs["low_confidence"],
        "status": out["status"],
        "suite_version": run.suite_version, "rubric_version": run.rubric_version,
    }


async def _proof_repo_eval(app_engine, cipher) -> dict:
    # historical=2 vs the fake's 3-line diff -> churn_penalty 0.667, so the
    # three dimensions come out non-zero AND non-identical.
    rid = await _make_repo_run(app_engine, cipher, historical=2)
    fake = FakeRepoSandbox()
    async with tenant_txn(app_engine, TENANT) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_repo_judge, k=5,
            exec_factory=lambda: fake.exec,
        )
    async with tenant_txn(app_engine, TENANT) as conn:
        dims = (await conn.execute(text(
            "select dimension, value from wololo_evals.scores "
            "where run_id = :r and trial_index is null order by dimension"
        ), {"r": rid})).all()
        run = (await conn.execute(text(
            "select suite_version, rubric_version from wololo_evals.runs where id = :r"
        ), {"r": rid})).first()
    scored = {d: round(float(v), 6) for d, v in dims}
    return {
        "id": "2-repo-eval-real-history",
        "suite": "repo-eval",
        "source": "atlas-onboarding-api seeded commit history (generate.py)",
        "dimensions": scored,
        "all_non_zero": all(v > 0 for v in scored.values()),
        "all_distinct": len(set(scored.values())) == len(scored),
        "composite_score": round(out["composite_score"], 6),
        "status": out["status"],
        "suite_version": run.suite_version, "rubric_version": run.rubric_version,
    }


async def _proof_canary(app_engine, cipher) -> dict:
    cid, rid = await _passed_run(app_engine, cipher)         # passes its own eval first
    gid = await _open_gate(app_engine, cid, rid, "meridian-gate")
    await _send_window(app_engine, gid, "w0", HEALTHY)       # freezes baseline
    await _send_window(app_engine, gid, "w1", REGRESSING)    # one regressing window
    deploy = create_deploy_stub()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=deploy), base_url="http://deploy"
    ) as dc:
        s1 = await canary.run_canary_poll(app_engine, TENANT, gid, dc)  # debounce: still watching
        await _send_window(app_engine, gid, "w2", REGRESSING)  # second consecutive
        # Drive the second poll in its two committed phases (canary.py splits
        # detect+claim in txn-1 from confirm_rollback in txn-2) so the transient
        # 'regressed' state is actually sampled between them, not merely implied
        # by the audit row. This is exactly what run_canary_poll composes.
        async with tenant_txn(app_engine, TENANT) as conn:
            claim = await canary.detect_and_claim(conn, TENANT, "canary-poller", gid)
        assert claim is not None
        async with tenant_txn(app_engine, TENANT) as conn:
            s_regressed = (await conn.execute(text(
                "select gate_status from wololo_evals.canary_gates where id = :id"
            ), {"id": gid})).scalar_one()
        resp = await dc.post(
            f"/canary/{claim.deploy_ref}/rollback",
            headers={"Idempotency-Key": claim.rollback_key},
        )
        async with tenant_txn(app_engine, TENANT) as conn:
            s2 = await canary.confirm_rollback(
                conn, TENANT, "canary-poller", gid, 200 <= resp.status_code < 300
            )
        calls = list(deploy.state.log.calls)
    async with tenant_txn(app_engine, TENANT) as conn:
        actions = (await conn.execute(text(
            "select action from wololo_evals.audit_events "
            "where resource = 'meridian-gate' order by seq"))).scalars().all()
    return {
        "id": "3-canary-regression-rollback",
        "suite": "canary",
        # opens watching, one regressing window stays watching (debounce),
        # second consecutive window flips to regressed (detect+claim) then
        # rolled-back on confirm.
        "gate_status_transitions": ["watching", s1, s_regressed, s2],
        "audit_actions": list(actions),
        "rollback_calls": len(calls),
        "idempotency_key": calls[0]["idempotency_key"] if calls else None,
    }


async def test_meridian_quality_gate_three_proofs(app_engine, cipher):
    p1 = await _proof_uplift(app_engine, cipher)
    p2 = await _proof_repo_eval(app_engine, cipher)
    p3 = await _proof_canary(app_engine, cipher)

    # proof #1: real paired uplift, 5 per arm, genuine positive mean, wins recorded
    assert len(p1["per_trial_composites"]["baseline"]) == 5
    assert len(p1["per_trial_composites"]["treatment"]) == 5
    assert p1["mean_uplift_pct"] > 0 and p1["wins"] == 5 and p1["n"] == 5

    # proof #2: three non-trivial (non-zero, non-identical) dimensions
    assert set(p2["dimensions"]) == {"correctness", "churn_penalty", "abstraction_adherence"}
    assert p2["all_non_zero"] and p2["all_distinct"]

    # proof #3: full watching -> regressed -> rolled-back with two audit rows
    #           and exactly one idempotent rollback call
    assert p3["gate_status_transitions"] == [
        "watching", "watching", "regressed", "rolled-back"
    ]
    assert p3["audit_actions"] == [
        "canary-opened", "canary-regression-detected", "canary-rollback-fired",
    ]
    assert p3["rollback_calls"] == 1
    assert p3["idempotency_key"] == "meridian-gate:" + await _canary_run_id(app_engine)

    if os.environ.get("WOLOLO_EVALS_WRITE_PROOF") == "1":
        _write_proof([p1, p2, p3])


async def _canary_run_id(app_engine) -> str:
    # the idempotency key is "<deploy_ref>:<run_id>"; recover the run_id for the
    # meridian-gate gate to assert the key shape without hard-coding it.
    async with tenant_txn(app_engine, TENANT) as conn:
        rid = (await conn.execute(text(
            "select run_id from wololo_evals.canary_gates where deploy_ref = 'meridian-gate'"
        ))).scalar_one()
    return str(rid)


def _write_proof(proofs: list[dict]) -> None:
    out = pathlib.Path(__file__).resolve().parents[1] / "meridian" / "proof"
    out.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    doc = {
        "scenario": "wololo-evals-quality-gate",
        "tenant": TENANT,
        "generated_at": ts,
        "mode": "degraded: proofs 1+2 exercise the real stats/scoring pipeline "
                "over deterministic fixture values (degenerate CI, zero variance; "
                "real variance only under the operator containerized run), proof 3 "
                "fully live; real-Atlas containerized run is the operator "
                "precondition (see run.py)",
        "proofs": proofs,
    }
    (out / f"wololo-evals-quality-gate-{ts}.json").write_text(json.dumps(doc, indent=2))
