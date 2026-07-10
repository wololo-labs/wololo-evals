"""FastAPI surface (SPEC S12/S13), wave-1 routes.

Tenant identity comes from the entitlement token on every route; a
``tenant_id`` in a body or query NEVER overrides it. Auth precedes every
database touch (the row-count-unchanged invariant tests prove the ordering).
The canary-gate routes drive the ``wololo-deploy`` seam, which ships today as
a documented stub (``stubs/deploy_stub.py`` + specs/eval-result/README.md),
named integration debt for 06a/06c.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
import uuid
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from . import canary, versioning
from .audit import append_audit_event
from .auth import (
    ROLE_ADMIN,
    ROLE_GATEKEEPER,
    ROLE_SUBMIT,
    AuthError,
    Principal,
    Verifier,
    bearer_token,
)
from .contracts.eval_candidate import build_eval_candidate
from .db import tenant_txn
from .jobs import queue
from .otel import record_metering, tracer
from .suites import base as suites_base
from .suites.repo_eval import generate as repo_gen

# All four suites are operator-reachable. repo-eval additionally generates its
# fixture from real commit history at run creation (see create_run); regression-
# replay reads its corpus from the candidate payload the operator submits.
EVAL_SUITES = ("skill-review", "task-uplift", "repo-eval", "regression-replay")
N_TRIALS_FLOOR = 5


@dataclass
class AppDeps:
    engine: AsyncEngine          # wololo_evals_app role: RLS-scoped
    verifier: Verifier
    cipher: object               # EnvelopeCipher
    blobstore: object


def create_app(deps: AppDeps) -> FastAPI:
    app = FastAPI(title="wololo-evals", version="0.1.0")
    app.state.deps = deps

    def principal_dep(authorization: str | None = Header(default=None)) -> Principal:
        return deps.verifier.verify(bearer_token(authorization))

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.middleware("http")
    async def _span(request: Request, call_next):
        with tracer().start_as_current_span(
            f"{request.method} {request.url.path}"
        ) as span:
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            tenant = getattr(request.state, "tenant_id", None)
            if tenant:
                span.set_attribute("tenant_id", tenant)
            return response

    async def _quota_guard(conn, principal: Principal, table: str, resource: str):
        """429 past tenants.quota_runs_per_day, audited + metered (S11)."""
        row = (
            await conn.execute(
                text(
                    f"""
                    select
                      (select quota_runs_per_day from wololo_evals.tenants
                        where tenant_id = :t) as quota,
                      (select count(*) from wololo_evals.{table}
                        where tenant_id = :t
                          and {'submitted_at' if table == 'candidates' else 'created_at'}
                              >= date_trunc('day', now())) as used
                    """
                ),
                {"t": principal.tenant_id},
            )
        ).first()
        if row.quota is not None and row.used >= row.quota:
            await append_audit_event(
                conn, principal.tenant_id, principal.actor,
                "quota-rejected", resource, decision="denied",
            )
            record_metering(
                principal.tenant_id, "submission", 1, **{"metering.rejected": True}
            )
            now = dt.datetime.now(dt.timezone.utc)
            midnight = (now + dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            retry_after = int((midnight - now).total_seconds())
            return JSONResponse(
                status_code=429,
                content={"detail": "daily run quota exhausted"},
                headers={"Retry-After": str(retry_after)},
            )
        return None

    # -- candidates ---------------------------------------------------------
    @app.post("/api/v1/candidates", status_code=201)
    async def submit_candidate(
        body: dict, request: Request, principal: Principal = Depends(principal_dep)
    ):
        principal.require(ROLE_SUBMIT)
        request.state.tenant_id = principal.tenant_id
        try:
            record = build_eval_candidate(
                tenant_id=principal.tenant_id,          # token, never body
                candidate_type=body.get("candidate_type", ""),
                candidate_ref=body.get("candidate_ref", ""),
                proposed_by=principal.actor,            # server-derived
                baseline_ref=body.get("baseline_ref", ""),
                payload=body.get("payload") or {},
                rationale=body.get("rationale"),
                target_eval_suite=body.get("target_eval_suite"),
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            rejected = await _quota_guard(conn, principal, "candidates", "candidate")
            if rejected is not None:
                return rejected
            ciphertext, key_id, payload_sha = deps.cipher.encrypt_payload(
                principal.tenant_id, record["payload"]
            )
            cid = str(uuid.uuid4())
            await conn.execute(
                text(
                    "insert into wololo_evals.candidates "
                    "(id, tenant_id, candidate_type, candidate_ref, proposed_by, "
                    " baseline_ref, payload_ciphertext, payload_key_id, payload_sha256, "
                    " rationale, target_eval_suite) "
                    "values (:id, :t, :ct, :cr, :pb, :br, :pc, :pk, :ps, :ra, :ts)"
                ),
                {
                    "id": cid, "t": principal.tenant_id,
                    "ct": record["candidate_type"], "cr": record["candidate_ref"],
                    "pb": record["proposed_by"], "br": record["baseline_ref"],
                    "pc": ciphertext, "pk": key_id, "ps": payload_sha,
                    "ra": record.get("rationale"),
                    "ts": record.get("target_eval_suite"),
                },
            )
            await append_audit_event(
                conn, principal.tenant_id, principal.actor,
                "candidate-submitted", cid,
            )
            record_metering(principal.tenant_id, "candidate", 1)
        return {
            "id": cid,
            "candidate_type": record["candidate_type"],
            "candidate_ref": record["candidate_ref"],
            "payload_sha256": payload_sha,
        }

    # -- runs -----------------------------------------------------------------
    @app.post("/api/v1/candidates/{candidate_id}/runs", status_code=201)
    async def create_run(
        candidate_id: str, body: dict, request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        principal.require(ROLE_SUBMIT)
        request.state.tenant_id = principal.tenant_id
        eval_suite = body.get("eval_suite", "")
        if eval_suite not in EVAL_SUITES:
            return JSONResponse(
                status_code=400,
                content={"detail": f"unknown eval_suite {eval_suite!r}"},
            )
        smoke = bool(body.get("smoke", False))
        # repo-eval generates its fixture from real commit history HERE, on the
        # host, at run creation (git plumbing must never run in the trial
        # sandbox). The redacted fixture rides the job payload to the worker.
        repo_eval_fixture = None
        if eval_suite == "repo-eval":
            src = body.get("repo_eval_source") or {}
            repo_path = src.get("repo_path")
            if not repo_path:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "repo-eval requires repo_eval_source.repo_path"},
                )
            try:
                fixture = repo_gen.generate(
                    repo_path,
                    ref=src.get("ref", "HEAD"),
                    repo_clone_source=src.get("repo_clone_source"),
                    acceptance_command=src.get("acceptance_command"),
                )
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"repo-eval fixture generation failed: {exc}"},
                )
            repo_eval_fixture = dataclasses.asdict(fixture)
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            cand = (
                await conn.execute(
                    text(
                        "select id from wololo_evals.candidates "
                        "where id = :id and tenant_id = :t"
                    ),
                    {"id": candidate_id, "t": principal.tenant_id},
                )
            ).first()
            if cand is None:
                return JSONResponse(status_code=404, content={"detail": "no such candidate"})
            tenant_row = (
                await conn.execute(
                    text("select n_trials from wololo_evals.tenants where tenant_id = :t"),
                    {"t": principal.tenant_id},
                )
            ).first()
            n_trials = int(body.get("n_trials") or tenant_row.n_trials)
            if n_trials < N_TRIALS_FLOOR and not smoke:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": f"n_trials {n_trials} below tenant floor {N_TRIALS_FLOOR}"
                    },
                )
            rejected = await _quota_guard(conn, principal, "runs", "run")
            if rejected is not None:
                return rejected
            # version pinning at creation (SPEC S9), rubric archived
            # content-addressed so old versions stay resolvable
            s_version = versioning.suite_version(suites_base.suite_dir(eval_suite))
            r_version = versioning.rubric_version(suites_base.rubric_path(eval_suite))
            versioning.archive_rubric(
                deps.blobstore, principal.tenant_id, suites_base.rubric_path(eval_suite)
            )
            run_id = str(uuid.uuid4())
            await conn.execute(
                text(
                    "insert into wololo_evals.runs "
                    "(id, tenant_id, candidate_id, eval_suite, status, suite_version, "
                    " rubric_version, n_trials, eval_log_prefix, smoke) "
                    "values (:id, :t, :c, :s, 'queued', :sv, :rv, :n, :lp, :sm)"
                ),
                {
                    "id": run_id, "t": principal.tenant_id, "c": candidate_id,
                    "s": eval_suite, "sv": s_version, "rv": r_version,
                    "n": n_trials,
                    "lp": f"evals/{principal.tenant_id}/{run_id}/",
                    "sm": smoke,
                },
            )
            await queue.enqueue(
                conn, principal.tenant_id, "eval-run",
                {
                    "run_id": run_id,
                    "task_fixture_ref": body.get("task_fixture_ref"),
                    # generated at run creation; None for the other suites
                    "repo_eval_fixture": repo_eval_fixture,
                },
                f"eval-run:{run_id}",
            )
            record_metering(principal.tenant_id, "run-enqueued", 1)
        return {
            "id": run_id, "status": "queued", "eval_suite": eval_suite,
            "n_trials": n_trials, "suite_version": s_version,
            "rubric_version": r_version, "smoke": smoke,
        }

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(
        run_id: str, request: Request, principal: Principal = Depends(principal_dep)
    ):
        request.state.tenant_id = principal.tenant_id
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            run = (
                await conn.execute(
                    text(
                        "select id, candidate_id, eval_suite, status, suite_version, "
                        "rubric_version, n_trials, composite_score, verdict_stats, "
                        "eval_log_prefix, smoke, started_at, completed_at "
                        "from wololo_evals.runs where id = :id"
                    ),
                    {"id": run_id},
                )
            ).first()
            if run is None:
                return JSONResponse(status_code=404, content={"detail": "no such run"})
            scores = (
                await conn.execute(
                    text(
                        "select dimension, value, weight, suite_version, rubric_version, judge "
                        "from wololo_evals.scores "
                        "where run_id = :id and trial_index is null order by dimension"
                    ),
                    {"id": run_id},
                )
            ).all()
        stats = run.verdict_stats
        if isinstance(stats, str):
            stats = json.loads(stats)
        return {
            "id": str(run.id),
            "candidate_id": str(run.candidate_id),
            "eval_suite": run.eval_suite,
            "status": run.status,
            "suite_version": run.suite_version,
            "rubric_version": run.rubric_version,
            "n_trials": run.n_trials,
            "composite_score": run.composite_score,
            "verdict_stats": stats,
            "eval_log_prefix": run.eval_log_prefix,
            "smoke": run.smoke,
            "scores": [dict(s._mapping) for s in scores],
        }

    @app.get("/api/v1/runs/{run_id}/trials")
    async def get_trials(
        run_id: str, request: Request, principal: Principal = Depends(principal_dep)
    ):
        request.state.tenant_id = principal.tenant_id
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            rows = (
                await conn.execute(
                    text(
                        "select trial_index, arm, status, model, harness, composite, "
                        "eval_log_path from wololo_evals.trials "
                        "where run_id = :id order by arm, trial_index"
                    ),
                    {"id": run_id},
                )
            ).all()
        return {"trials": [dict(r._mapping) for r in rows]}

    # -- leaderboard ----------------------------------------------------------
    @app.get("/api/v1/leaderboard")
    async def leaderboard(
        request: Request,
        harness: str | None = None,
        skill_ref: str | None = None,
        rubric_version: str | None = None,
        principal: Principal = Depends(principal_dep),
    ):
        request.state.tenant_id = principal.tenant_id
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            if rubric_version is None:
                latest = (
                    await conn.execute(
                        text(
                            "select rubric_version from wololo_evals.leaderboard "
                            "order by computed_at desc limit 1"
                        )
                    )
                ).first()
                if latest is None:
                    return {"rubric_version": None, "entries": []}
                rubric_version = latest.rubric_version
            filters = "and harness = :h " if harness else ""
            filters += "and skill_ref = :sr " if skill_ref else ""
            params = {"rv": rubric_version}
            if harness:
                params["h"] = harness
            if skill_ref:
                params["sr"] = skill_ref
            rows = (
                await conn.execute(
                    text(
                        "select model, harness, skill_ref, composite_score, n_trials, "
                        "score_stddev, ci_low, ci_high, suite_version, rubric_version, "
                        "run_id, computed_at from wololo_evals.leaderboard "
                        f"where rubric_version = :rv {filters}"
                        "order by composite_score desc"
                    ),
                    params,
                )
            ).all()
        return {
            "rubric_version": rubric_version,
            "entries": [
                {**dict(r._mapping), "run_id": str(r.run_id),
                 "computed_at": r.computed_at.isoformat()}
                for r in rows
            ],
        }

    @app.post("/api/v1/leaderboard/rebuild")
    async def rebuild_leaderboard(
        request: Request, principal: Principal = Depends(principal_dep)
    ):
        principal.require(ROLE_ADMIN)
        request.state.tenant_id = principal.tenant_id
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            await conn.execute(text("delete from wololo_evals.leaderboard"))
            inserted = await conn.execute(
                text(
                    """
                    insert into wololo_evals.leaderboard
                      (tenant_id, model, harness, skill_ref, composite_score, n_trials,
                       score_stddev, ci_low, ci_high, suite_version, rubric_version, run_id)
                    select r.tenant_id,
                           coalesce((select max(t.model) from wololo_evals.trials t
                                      where t.run_id = r.id), 'unknown'),
                           coalesce((select max(t.harness) from wololo_evals.trials t
                                      where t.run_id = r.id), 'static'),
                           c.candidate_ref,
                           r.composite_score,
                           r.n_trials,
                           (r.verdict_stats->>'stddev')::real,
                           (r.verdict_stats->>'ci_low')::real,
                           (r.verdict_stats->>'ci_high')::real,
                           r.suite_version, r.rubric_version, r.id
                    from wololo_evals.runs r
                    join wololo_evals.candidates c on c.id = r.candidate_id
                    where r.status in ('passed','failed')
                      and r.composite_score is not null
                      and not r.smoke
                    """
                )
            )
            await append_audit_event(
                conn, principal.tenant_id, principal.actor,
                "leaderboard-rebuilt", f"rows:{inserted.rowcount}",
            )
        return {"rebuilt": inserted.rowcount}

    # -- canary gates (the wololo-deploy seam, SPEC S5.5) ------------------
    @app.post("/api/v1/canary-gates", status_code=201)
    async def open_canary_gate(
        body: dict, request: Request, principal: Principal = Depends(principal_dep)
    ):
        principal.require(ROLE_GATEKEEPER)
        request.state.tenant_id = principal.tenant_id
        candidate_id = body.get("candidate_id", "")
        run_id = body.get("run_id", "")
        deploy_ref = body.get("deploy_ref", "")
        if not (candidate_id and run_id and deploy_ref):
            return JSONResponse(
                status_code=400,
                content={"detail": "candidate_id, run_id and deploy_ref are required"},
            )
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            try:
                gate = await canary.open_gate(
                    conn, principal.tenant_id, principal.actor,
                    candidate_id, run_id, deploy_ref,
                )
            except canary.GateError as exc:
                return JSONResponse(status_code=409, content={"detail": str(exc)})
        return gate

    @app.post("/api/v1/canary-gates/{gate_id}/telemetry", status_code=202)
    async def post_canary_telemetry(
        gate_id: str,
        body: dict,
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        principal.require(ROLE_GATEKEEPER)
        request.state.tenant_id = principal.tenant_id
        metrics = body.get("metrics") or {}
        # A self-reported composite is never accepted; the gate computes it.
        if "composite" in body or "composite" in metrics:
            return JSONResponse(
                status_code=422,
                content={"detail": "telemetry carries raw metrics only, not a composite"},
            )
        for field in ("window_id", "sample_count", "observed_at"):
            if body.get(field) in (None, ""):
                return JSONResponse(
                    status_code=400, content={"detail": f"{field} is required"}
                )
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            try:
                result = await canary.ingest_telemetry(
                    conn, principal.tenant_id, gate_id,
                    body["window_id"], metrics, int(body["sample_count"]),
                    body["observed_at"],
                )
            except canary.GateError as exc:
                return JSONResponse(status_code=404, content={"detail": str(exc)})
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
            # Regression evaluation runs as a durable canary-poll job, never an
            # in-process timer (SPEC S5.5): one poll per window, on the queue.
            await queue.enqueue(
                conn, principal.tenant_id, "canary-poll",
                {"gate_id": gate_id, "window_id": body["window_id"]},
                f"canary-poll:{gate_id}:{body['window_id']}",
            )
        return result

    @app.get("/api/v1/canary-gates/{gate_id}/verdict")
    async def get_canary_verdict(
        gate_id: str, request: Request, principal: Principal = Depends(principal_dep)
    ):
        request.state.tenant_id = principal.tenant_id
        async with tenant_txn(deps.engine, principal.tenant_id) as conn:
            gate = (
                await conn.execute(
                    text(
                        "select gate_status, baseline_composite "
                        "from wololo_evals.canary_gates where id = :id"
                    ),
                    {"id": gate_id},
                )
            ).first()
            if gate is None:
                return JSONResponse(status_code=404, content={"detail": "no such gate"})
            latest = (
                await conn.execute(
                    text(
                        "select live_composite, count(*) over () as observed "
                        "from wololo_evals.canary_telemetry where gate_id = :id "
                        "order by observed_at desc limit 1"
                    ),
                    {"id": gate_id},
                )
            ).first()
        return {
            "gate_status": gate.gate_status,
            "live_composite": latest.live_composite if latest else None,
            "baseline_composite": gate.baseline_composite,
            "windows_observed": latest.observed if latest else 0,
        }

    return app
