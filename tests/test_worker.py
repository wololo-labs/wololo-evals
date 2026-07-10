"""End-to-end skill-review orchestration on real Postgres with a deterministic
injected judge (no network, no docker): candidate -> encrypted at rest ->
enqueued -> worker leases -> k judge samples -> trials + per-sample score rows
+ aggregate rows -> frozen composite + verdict_stats -> leaderboard row.

The real judge model call and the task-uplift harness sandbox are OPERATOR
preconditions for this wave; the injected ``call_model`` proves the whole
pipeline shape deterministically.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from wololo_evals.crypto import EnvelopeCipher
from wololo_evals.db import tenant_txn
from wololo_evals.jobs import queue
from wololo_evals.jobs.worker import execute_run, run_once
from wololo_evals.stubs.candidate_fixture_gen import skill_candidate
from wololo_evals.suites import base as suites_base
from wololo_evals import versioning


def _judge(scores: dict[str, float], *, injection: bool = False):
    """A deterministic judge: ignores the prompt, returns fixed dimension
    scores as schema-valid JSON. This is the injected ``call_model``."""

    def call(system: str, user: str) -> str:
        return json.dumps(
            {
                "dimension_scores": scores,
                "rationale": "deterministic test judge",
                "injection_suspected": injection,
            }
        )

    return call


DIMS = ["validation_gate", "implementation", "activation", "review"]


async def _make_candidate(engine, cipher, tenant_id, variant="good") -> str:
    rec = skill_candidate(tenant_id, variant=variant)
    ct, kid, sha = cipher.encrypt_payload(tenant_id, rec["payload"])
    cid = str(uuid.uuid4())
    async with tenant_txn(engine, tenant_id) as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.candidates "
                "(id, tenant_id, candidate_type, candidate_ref, proposed_by, "
                " baseline_ref, payload_ciphertext, payload_key_id, payload_sha256) "
                "values (:id,:t,:ct,:cr,:pb,:br,:pc,:pk,:ps)"
            ),
            {
                "id": cid, "t": tenant_id, "ct": rec["candidate_type"],
                "cr": rec["candidate_ref"], "pb": rec["proposed_by"],
                "br": rec["baseline_ref"], "pc": ct, "pk": kid, "ps": sha,
            },
        )
    return cid


async def _make_run(engine, tenant_id, candidate_id) -> str:
    rid = str(uuid.uuid4())
    sv = versioning.suite_version(suites_base.suite_dir("skill-review"))
    rv = versioning.rubric_version(suites_base.rubric_path("skill-review"))
    async with tenant_txn(engine, tenant_id) as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.runs "
                "(id, tenant_id, candidate_id, eval_suite, status, suite_version, "
                " rubric_version, n_trials, eval_log_prefix) "
                "values (:id,:t,:c,'skill-review','queued',:sv,:rv,5,:lp)"
            ),
            {"id": rid, "t": tenant_id, "c": candidate_id, "sv": sv, "rv": rv,
             "lp": f"evals/{tenant_id}/{rid}/"},
        )
    return rid


@pytest.fixture()
def cipher(tmp_path):
    return EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "keyring.json"))


async def test_skill_review_full_pipeline(app_engine, cipher):
    tenant = "meridian"
    cid = await _make_candidate(app_engine, cipher, tenant, variant="good")
    rid = await _make_run(app_engine, tenant, cid)

    high = {d: 0.9 for d in DIMS}
    async with tenant_txn(app_engine, tenant) as conn:
        out = await execute_run(conn, rid, cipher=cipher, call_model=_judge(high), k=5)

    assert out["status"] == "passed"  # 0.9 composite >= 0.7 pass_bar
    assert out["composite_score"] == pytest.approx(0.9)

    async with tenant_txn(app_engine, tenant) as conn:
        run = (await conn.execute(text(
            "select status, composite_score, verdict_stats from wololo_evals.runs "
            "where id = :id"), {"id": rid})).first()
        trials = (await conn.execute(text(
            "select count(*) from wololo_evals.trials where run_id = :id"),
            {"id": rid})).scalar_one()
        agg = (await conn.execute(text(
            "select count(*) from wololo_evals.scores where run_id = :id "
            "and trial_index is null"), {"id": rid})).scalar_one()
        per_trial = (await conn.execute(text(
            "select count(*) from wololo_evals.scores where run_id = :id "
            "and trial_index is not null"), {"id": rid})).scalar_one()
        lb = (await conn.execute(text(
            "select composite_score, n_trials, rubric_version from wololo_evals.leaderboard "
            "where run_id = :id"), {"id": rid})).first()

    assert run.status == "passed"
    assert run.composite_score == pytest.approx(0.9)
    stats = run.verdict_stats if isinstance(run.verdict_stats, dict) else json.loads(run.verdict_stats)
    assert set(stats) >= {"mean", "stddev", "ci_low", "ci_high", "n", "low_confidence"}
    assert trials == 5                       # one per judge sample
    assert agg == 4                          # median-per-dimension aggregate rows
    assert per_trial == 5 * 4                # every raw sample stored
    assert lb.n_trials == 5
    assert lb.composite_score == pytest.approx(0.9)


async def test_thin_skill_fails_below_pass_bar(app_engine, cipher):
    tenant = "meridian"
    cid = await _make_candidate(app_engine, cipher, tenant, variant="thin")
    rid = await _make_run(app_engine, tenant, cid)
    low = {"validation_gate": 0.1, "implementation": 0.3, "activation": 0.3, "review": 0.1}
    async with tenant_txn(app_engine, tenant) as conn:
        out = await execute_run(conn, rid, cipher=cipher, call_model=_judge(low), k=5)
    assert out["status"] == "failed"  # weighted composite well below 0.7


async def test_injection_tripwire_errors_run(app_engine, cipher):
    tenant = "meridian"
    cid = await _make_candidate(app_engine, cipher, tenant, variant="injection")
    rid = await _make_run(app_engine, tenant, cid)
    high = {d: 1.0 for d in DIMS}
    # The judge flags injection: run must error, never auto-pass at 1.0.
    async with tenant_txn(app_engine, tenant) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_judge(high, injection=True), k=5
        )
    assert out["status"] == "errored"
    assert out["reason"] == "judge-integrity"

    async with tenant_txn(app_engine, tenant) as conn:
        run_status = (await conn.execute(text(
            "select status from wololo_evals.runs where id = :id"),
            {"id": rid})).scalar_one()
        flagged = (await conn.execute(text(
            "select count(*) from wololo_evals.audit_events where resource = :id "
            "and action = 'judge-integrity-flagged' and decision = 'denied'"),
            {"id": rid})).scalar_one()
    assert run_status == "errored"
    assert flagged == 1


async def test_run_once_dispatches_from_queue(app_engine, worker_engine, cipher):
    """The durable queue -> worker -> completion path, end to end."""
    tenant = "meridian"
    cid = await _make_candidate(app_engine, cipher, tenant, variant="good")
    rid = await _make_run(app_engine, tenant, cid)
    async with tenant_txn(app_engine, tenant) as conn:
        await queue.enqueue(conn, tenant, "eval-run", {"run_id": rid}, f"eval-run:{rid}")

    high = {d: 0.85 for d in DIMS}
    job = await run_once(worker_engine, cipher=cipher, call_model=_judge(high),
                         worker_id="w-test")
    assert job is not None and job["payload"]["run_id"] == rid

    async with tenant_txn(app_engine, tenant) as conn:
        status = (await conn.execute(text(
            "select status from wololo_evals.runs where id = :id"),
            {"id": rid})).scalar_one()
        jobstatus = (await conn.execute(text(
            "select status from wololo_evals.jobs where idempotency_key = :k"),
            {"k": f"eval-run:{rid}"})).scalar_one()
    assert status == "passed"
    assert jobstatus == "done"
