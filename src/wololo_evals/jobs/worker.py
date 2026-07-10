"""Eval-run worker: turns a leased ``eval-run`` job into trials, scores, a
frozen composite, verdict stats, and a leaderboard row.

The skill-review path is fully live here: it decrypts the candidate, runs the
injection-hardened judge (k samples, median-per-dimension), stores every raw
sample as a per-trial score row, freezes the composite ONCE at completion, and
upserts the leaderboard. The task-uplift path needs the harness-under-eval
sandbox (``harness_solver`` + a docker daemon + a model API key); in an
environment without those it raises :class:`HarnessExecutionRequired` rather
than fabricating a verdict — an honest guard, not a placeholder (the execution
model is in ``harness_solver.py``; a live run is a documented OPERATOR
precondition for this wave).

``call_model`` is injected (``(system, user) -> str``) so the whole
orchestration is testable deterministically without a network judge.
"""
from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..audit import append_audit_event
from ..db import tenant_txn
from ..judge import run_judge
from ..otel import record_metering, tracer
from ..stats import single_arm_stats
from ..suites import base as suites_base
from . import queue


class HarnessExecutionRequired(Exception):
    """task-uplift needs the docker harness sandbox + a model API key; refuse
    to fabricate a verdict when they are absent (OPERATOR precondition)."""


async def execute_run(
    conn,
    run_id: str,
    *,
    cipher,
    call_model: Callable[[str, str], str],
    k: int = 5,
) -> dict:
    """Execute one run inside the caller's tenant-scoped transaction."""
    run = (
        await conn.execute(
            text(
                "select id, tenant_id, candidate_id, eval_suite, suite_version, "
                "rubric_version, n_trials, smoke "
                "from wololo_evals.runs where id = :id and status = 'queued'"
            ),
            {"id": run_id},
        )
    ).first()
    if run is None:
        return {"status": "skipped", "reason": "run not queued"}

    tenant_id = run.tenant_id
    await conn.execute(
        text(
            "update wololo_evals.runs set status = 'running', started_at = now() "
            "where id = :id"
        ),
        {"id": run_id},
    )

    if run.eval_suite != "skill-review":
        # Verdict-bearing task-uplift requires the harness sandbox; do not fake.
        await conn.execute(
            text(
                "update wololo_evals.runs set status = 'errored', completed_at = now() "
                "where id = :id"
            ),
            {"id": run_id},
        )
        raise HarnessExecutionRequired(
            f"suite {run.eval_suite!r} needs the docker harness sandbox + model key"
        )

    with tracer().start_as_current_span("eval-run") as span:
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("run_id", str(run_id))
        span.set_attribute("eval_suite", run.eval_suite)
        return await _run_skill_review(conn, run, cipher, call_model, k)


async def _run_skill_review(conn, run, cipher, call_model, k) -> dict:
    run_id = str(run.id)
    tenant_id = run.tenant_id
    cand = (
        await conn.execute(
            text(
                "select candidate_ref, payload_ciphertext, payload_sha256 "
                "from wololo_evals.candidates where id = :id"
            ),
            {"id": run.candidate_id},
        )
    ).first()
    payload = cipher.decrypt_payload(
        tenant_id, cand.payload_ciphertext, cand.payload_sha256
    )
    content = payload.get("SKILL.md") or next(
        (v for v in payload.values() if isinstance(v, str)), ""
    )

    rubric = suites_base.load_rubric("skill-review")
    dimensions = suites_base.judged_dimensions(rubric)  # all four
    rubric_text = suites_base.rubric_judge_text(rubric, dimensions)

    result = run_judge(call_model, rubric_text, content, dimensions, k=k)
    record_metering(tenant_id, "judge-samples", k)

    if result.injection_suspected:
        await conn.execute(
            text(
                "update wololo_evals.runs set status = 'errored', completed_at = now() "
                "where id = :id"
            ),
            {"id": run_id},
        )
        await append_audit_event(
            conn, tenant_id, "worker", "judge-integrity-flagged", run_id,
            decision="denied",
        )
        return {"status": "errored", "reason": "judge-integrity", "run_id": run_id}

    sample_composites = [
        suites_base.composite(rubric, s["dimension_scores"]) for s in result.samples
    ]
    agg_composite = suites_base.composite(rubric, result.dimension_medians)

    tenant_row = (
        await conn.execute(
            text("select pass_bar from wololo_evals.tenants where tenant_id = :t"),
            {"t": tenant_id},
        )
    ).first()
    pass_bar = tenant_row.pass_bar
    vstats = single_arm_stats(sample_composites, pass_bar)

    # Per-sample trials (arm 'single') + every raw judge sample as a per-trial
    # score row (SPEC S5.1: all k samples stored).
    for i, sample in enumerate(result.samples):
        await conn.execute(
            text(
                "insert into wololo_evals.trials "
                "(tenant_id, run_id, trial_index, arm, status, composite, "
                " started_at, completed_at) "
                "values (:t, :r, :ti, 'single', 'completed', :c, now(), now())"
            ),
            {"t": tenant_id, "r": run_id, "ti": i, "c": sample_composites[i]},
        )
        for dim, val in sample["dimension_scores"].items():
            await _score(conn, tenant_id, run_id, dim, val, run, trial_index=i,
                         judge="injected-judge")

    # Aggregate (trial_index null) rows = the median-per-dimension the composite
    # is frozen from.
    for dim, val in result.dimension_medians.items():
        await _score(conn, tenant_id, run_id, dim, val, run, trial_index=None,
                     weight=rubric["dimensions"][dim]["weight"], judge="injected-judge")

    status = "passed" if agg_composite >= pass_bar else "failed"
    await conn.execute(
        text(
            "update wololo_evals.runs set status = :s, composite_score = :c, "
            "verdict_stats = cast(:v as jsonb), completed_at = now() where id = :id"
        ),
        {"s": status, "c": agg_composite, "v": json.dumps(vstats), "id": run_id},
    )

    # Leaderboard read-model row (skill-review has no executing model/harness).
    await conn.execute(
        text(
            "insert into wololo_evals.leaderboard "
            "(tenant_id, model, harness, skill_ref, composite_score, n_trials, "
            " score_stddev, ci_low, ci_high, suite_version, rubric_version, run_id) "
            "values (:t, 'static', 'static', :sk, :cs, :n, :sd, :cl, :ch, :sv, :rv, :r) "
            "on conflict (tenant_id, model, harness, skill_ref, rubric_version, run_id) "
            "do nothing"
        ),
        {
            "t": tenant_id, "sk": cand.candidate_ref, "cs": agg_composite, "n": k,
            "sd": vstats["stddev"], "cl": vstats["ci_low"], "ch": vstats["ci_high"],
            "sv": run.suite_version, "rv": run.rubric_version, "r": run_id,
        },
    )

    await append_audit_event(conn, tenant_id, "worker", "run-completed", run_id)
    record_metering(tenant_id, "run", 1, **{"metering.composite": agg_composite})
    return {
        "status": status,
        "composite_score": agg_composite,
        "verdict_stats": vstats,
        "run_id": run_id,
    }


async def _score(conn, tenant_id, run_id, dimension, value, run, *,
                 trial_index=None, weight=1.0, judge=None) -> None:
    await conn.execute(
        text(
            "insert into wololo_evals.scores "
            "(tenant_id, run_id, trial_index, dimension, value, weight, "
            " suite_version, rubric_version, judge) "
            "values (:t, :r, :ti, :d, :v, :w, :sv, :rv, :j)"
        ),
        {
            "t": tenant_id, "r": run_id, "ti": trial_index, "d": dimension,
            "v": value, "w": weight, "sv": run.suite_version,
            "rv": run.rubric_version, "j": judge,
        },
    )


async def run_once(
    worker_engine: AsyncEngine,
    *,
    cipher,
    call_model: Callable[[str, str], str],
    worker_id: str = "worker-1",
) -> Optional[dict]:
    """Lease one job (worker role's global view), execute it under the job
    tenant's RLS scope, then mark it done/failed. Returns the job or None."""
    async with worker_engine.begin() as conn:
        job = await queue.dequeue(conn, worker_id)
    if job is None:
        return None
    try:
        if job["kind"] == "eval-run":
            run_id = job["payload"]["run_id"]
            async with tenant_txn(worker_engine, job["tenant_id"]) as wconn:
                await execute_run(wconn, run_id, cipher=cipher, call_model=call_model)
        async with worker_engine.begin() as conn:
            await queue.complete(conn, job["id"])
    except Exception:
        async with worker_engine.begin() as conn:
            await queue.fail(conn, job["id"])
        raise
    return job


def _call_model_from_settings():
    """Resolve the judge model call from config: an anthropic-compatible HTTP
    endpoint (default) or the claude CLI bridge for OAuth-only environments."""
    from ..judge import cli_call_model, http_call_model
    from ..settings import settings

    cfg = settings()
    if cfg.judge_backend == "cli":
        return cli_call_model(cfg.judge_model)
    return http_call_model(
        cfg.judge_model, base_url=cfg.judge_base_url, api_key=cfg.judge_api_key
    )


async def _main() -> None:
    import asyncio

    from ..crypto import EnvelopeCipher
    from ..db import make_engine
    from ..otel import init_tracing
    from ..settings import settings

    cfg = settings()
    init_tracing()
    engine = make_engine(cfg.database_url)
    cipher = EnvelopeCipher(cfg.master_key_hex, cfg.master_key_id, cfg.keyring_path)
    call_model = _call_model_from_settings()
    idle_backoff = 1.0
    while True:
        job = await run_once(engine, cipher=cipher, call_model=call_model)
        if job is None:
            await asyncio.sleep(idle_backoff)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
