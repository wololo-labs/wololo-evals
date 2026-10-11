"""Invariant 4 (entitlement precedes the write) + invariant 1 via HTTP +
quota enforcement, against the real FastAPI app on the real RLS Postgres.

The row-count-unchanged assertions are the load-bearing ones: a bad/absent/
wrong-role token must be rejected BEFORE any row is written, so the count of
``candidates`` is identical before and after a rejected request.
"""
from __future__ import annotations

import time

import httpx
import pytest
from sqlalchemy import text

from wololo_evals.api import AppDeps, create_app
from wololo_evals.auth import Verifier
from wololo_evals.crypto import EnvelopeCipher

from support import JWKS_PATH, mint_token

SUBMIT = ["wololo-evals:submit"]
GATEKEEPER = ["wololo-evals:gatekeeper"]


@pytest.fixture()
def deps(app_engine, tmp_path):
    cipher = EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "keyring.json"))
    from wololo_evals.blobstore import FsBlobStore

    return AppDeps(
        engine=app_engine,
        verifier=Verifier(JWKS_PATH, "wololo-deploy"),
        cipher=cipher,
        blobstore=FsBlobStore(str(tmp_path / "blobs")),
        repo_eval_roots=(str(tmp_path),),
    )


@pytest.fixture()
def client(deps):
    app = create_app(deps)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def _count(engine, tenant_id: str, table: str = "candidates") -> int:
    from wololo_evals.db import tenant_txn

    async with tenant_txn(engine, tenant_id) as conn:
        return (
            await conn.execute(
                text(f"select count(*) from wololo_evals.{table}")
            )
        ).scalar_one()


CANDIDATE_BODY = {
    "candidate_type": "skill",
    "candidate_ref": "simplify@1.2.0",
    "baseline_ref": "simplify@1.1.0",
    "payload": {"SKILL.md": "# simplify\nreuse before building."},
}


async def test_valid_submit_writes_one_row(client, app_engine):
    before = await _count(app_engine, "meridian")
    token = mint_token("meridian", SUBMIT)
    async with client:
        r = await client.post(
            "/api/v1/candidates",
            json=CANDIDATE_BODY,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 201, r.text
    assert r.json()["payload_sha256"]
    assert await _count(app_engine, "meridian") == before + 1


async def test_missing_token_rejected_row_unchanged(client, app_engine):
    before = await _count(app_engine, "meridian")
    async with client:
        r = await client.post("/api/v1/candidates", json=CANDIDATE_BODY)
    assert r.status_code == 401
    assert await _count(app_engine, "meridian") == before  # write never happened


async def test_expired_token_rejected_row_unchanged(client, app_engine):
    before = await _count(app_engine, "meridian")
    now = int(time.time())
    token = mint_token("meridian", SUBMIT, iat=now - 7200, exp=now - 3600)
    async with client:
        r = await client.post(
            "/api/v1/candidates",
            json=CANDIDATE_BODY,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 401
    assert await _count(app_engine, "meridian") == before


async def test_wrong_role_rejected_row_unchanged(client, app_engine):
    before = await _count(app_engine, "meridian")
    token = mint_token("meridian", GATEKEEPER)  # no submit feature
    async with client:
        r = await client.post(
            "/api/v1/candidates",
            json=CANDIDATE_BODY,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403
    assert await _count(app_engine, "meridian") == before


async def test_missing_required_field_is_400_not_defaulted(client, app_engine):
    before = await _count(app_engine, "meridian")
    token = mint_token("meridian", SUBMIT)
    body = {k: v for k, v in CANDIDATE_BODY.items() if k != "baseline_ref"}
    async with client:
        r = await client.post(
            "/api/v1/candidates",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400
    assert "baseline_ref" in r.text
    assert await _count(app_engine, "meridian") == before


async def test_body_tenant_id_cannot_override_token(client, app_engine):
    """A submit token for meridian carrying a northwind tenant_id in the body
    writes to meridian (the token claim is the only source)."""
    m_before = await _count(app_engine, "meridian")
    n_before = await _count(app_engine, "northwind")
    token = mint_token("meridian", SUBMIT)
    async with client:
        r = await client.post(
            "/api/v1/candidates",
            json={**CANDIDATE_BODY, "tenant_id": "northwind"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 201
    assert await _count(app_engine, "meridian") == m_before + 1
    assert await _count(app_engine, "northwind") == n_before  # not written to


async def test_cross_tenant_run_read_is_404(client, app_engine):
    """Northwind cannot read a Meridian run over the API (invariant 1, RLS)."""
    m_token = mint_token("meridian", SUBMIT)
    async with client:
        c = await client.post(
            "/api/v1/candidates",
            json=CANDIDATE_BODY,
            headers={"Authorization": f"Bearer {m_token}"},
        )
        cid = c.json()["id"]
        run = await client.post(
            f"/api/v1/candidates/{cid}/runs",
            json={"eval_suite": "skill-review", "n_trials": 5},
            headers={"Authorization": f"Bearer {m_token}"},
        )
        assert run.status_code == 201, run.text
        run_id = run.json()["id"]

        # Meridian sees its own run...
        seen = await client.get(
            f"/api/v1/runs/{run_id}", headers={"Authorization": f"Bearer {m_token}"}
        )
        assert seen.status_code == 200

        # ...Northwind does not.
        n_token = mint_token("northwind", SUBMIT)
        denied = await client.get(
            f"/api/v1/runs/{run_id}", headers={"Authorization": f"Bearer {n_token}"}
        )
    assert denied.status_code == 404


async def test_quota_429_with_audit_row(client, app_engine, owner_engine):
    # A fresh tenant with a quota of 1: the second submit today is 429.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.tenants (tenant_id, name, quota_runs_per_day) "
                "values ('quotatest', 'Quota Test', 1) "
                "on conflict (tenant_id) do update set quota_runs_per_day = 1"
            )
        )
        # clean any prior candidates from a re-run so the count starts at 0 today
        await conn.execute(
            text("delete from wololo_evals.candidates where tenant_id = 'quotatest'")
        )
        await conn.execute(
            text("delete from wololo_evals.audit_events where tenant_id = 'quotatest'")
        )

    token = mint_token("quotatest", SUBMIT)
    async with client:
        first = await client.post(
            "/api/v1/candidates",
            json=CANDIDATE_BODY,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/v1/candidates",
            json=CANDIDATE_BODY,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert second.status_code == 429
    assert second.headers.get("Retry-After")

    async with owner_engine.begin() as conn:
        rejected = (
            await conn.execute(
                text(
                    "select count(*) from wololo_evals.audit_events "
                    "where tenant_id = 'quotatest' and action = 'quota-rejected' "
                    "and decision = 'denied'"
                )
            )
        ).scalar_one()
    assert rejected == 1


# --- all four suites are operator-reachable through create_run --------------
async def _drain_job(worker_engine, run_id: str) -> None:
    """Retire the queued eval-run job this test left behind so another test's
    ``run_once`` does not grab it (the queue is round-robin oldest-first over
    the session-scoped database). The worker role has the global jobs view and
    the update grant used by real dequeue/complete."""
    async with worker_engine.begin() as conn:
        await conn.execute(
            text("update wololo_evals.jobs set status = 'done' where idempotency_key = :k"),
            {"k": f"eval-run:{run_id}"},
        )


async def test_repo_eval_run_operator_reachable_end_to_end(
    client, deps, app_engine, worker_engine, tmp_path
):
    """repo-eval is triggerable through the running service, not just a test-
    helper DB insert: submit a harness-config candidate, create a repo-eval run
    whose fixture the API generates from a real git repo, then drain the job and
    execute it to a verdict. Proves generate() runs on a real request path."""
    from wololo_evals.db import tenant_txn
    from wololo_evals.jobs.worker import execute_run

    from test_repo_eval import FakeRepoSandbox, _init_repo, _judge

    repo = tmp_path / "atlas-onboarding"
    _init_repo(repo)

    token = mint_token("meridian", SUBMIT)
    async with client:
        cand = await client.post(
            "/api/v1/candidates",
            json={
                "candidate_type": "harness-config",
                "candidate_ref": "claude-code@repo-eval",
                "baseline_ref": "claude-code@baseline",
                "payload": {"harness": "claude-code", "model": "claude-sonnet-4"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert cand.status_code == 201, cand.text
        cid = cand.json()["id"]
        run = await client.post(
            f"/api/v1/candidates/{cid}/runs",
            json={
                "eval_suite": "repo-eval",
                "n_trials": 5,
                "repo_eval_source": {"repo_path": str(repo)},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    # the fixture the API generated is on the enqueued job, with the oracle test
    async with tenant_txn(app_engine, "meridian") as conn:
        payload = (
            await conn.execute(
                text(
                    "select payload from wololo_evals.jobs "
                    "where idempotency_key = :k"
                ),
                {"k": f"eval-run:{run_id}"},
            )
        ).scalar_one()
    fx = payload["repo_eval_fixture"]
    assert fx is not None
    assert fx["oracle_files"] == ["test_app.py"]

    # drain the job through the worker exactly as run_once would (fake exec seam
    # so no docker daemon is needed), passing the job payload it carries
    fake = FakeRepoSandbox()
    async with tenant_txn(app_engine, "meridian") as conn:
        out = await execute_run(
            conn, run_id, cipher=deps.cipher, call_model=_judge, k=5,
            exec_factory=lambda: fake.exec, job_payload=payload,
        )
    assert out["status"] == "passed", out
    assert out["verdict_stats"]["n"] == 5
    await _drain_job(worker_engine, run_id)


async def test_regression_replay_run_is_accepted(client, app_engine, worker_engine):
    """regression-replay is no longer rejected at create_run (it was a 400
    before the suite was wired); the corpus rides the candidate payload."""
    token = mint_token("meridian", SUBMIT)
    async with client:
        cand = await client.post(
            "/api/v1/candidates",
            json={
                "candidate_type": "prompt",
                "candidate_ref": "reflect-prompt@2",
                "baseline_ref": "reflect-prompt@1",
                "payload": {"prompt": "always cite the correction reference"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert cand.status_code == 201, cand.text
        cid = cand.json()["id"]
        run = await client.post(
            f"/api/v1/candidates/{cid}/runs",
            json={"eval_suite": "regression-replay", "n_trials": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert run.status_code == 201, run.text
    assert run.json()["eval_suite"] == "regression-replay"
    await _drain_job(worker_engine, run.json()["id"])


async def test_repo_eval_refuses_a_repo_path_outside_the_allowed_roots(
    client, deps, tmp_path
):
    # repo_path is read with git on the host. Any submitter could name any
    # readable repository, so it must sit under an operator-chosen root.
    from test_repo_eval import _init_repo

    elsewhere = tmp_path.parent / (tmp_path.name + "-elsewhere")
    _init_repo(elsewhere)
    token = mint_token("meridian", SUBMIT)
    async with client:
        cand = await client.post(
            "/api/v1/candidates",
            json={
                "candidate_type": "harness-config",
                "candidate_ref": "claude-code@repo-eval-2",
                "baseline_ref": "claude-code@baseline",
                "payload": {"harness": "claude-code", "model": "claude-sonnet-4"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        cid = cand.json()["id"]
        for roots in ((), (str(tmp_path),)):
            deps.repo_eval_roots = roots
            run = await client.post(
                f"/api/v1/candidates/{cid}/runs",
                json={
                    "eval_suite": "repo-eval",
                    "n_trials": 5,
                    "repo_eval_source": {"repo_path": str(elsewhere)},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert run.status_code == 400, run.text
            assert "allowed root" in run.text
