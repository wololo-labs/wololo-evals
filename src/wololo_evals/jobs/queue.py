"""Durable Postgres job queue (SPEC S11).

All asynchronous work (eval dispatch, canary polls, retention sweeps) runs
through ``wololo_evals.jobs``: ``FOR UPDATE SKIP LOCKED`` dequeue, leases +
heartbeats, exponential backoff via ``run_after``, ``dead`` after
``max_attempts``, idempotency keys so a redelivered job is a no-op where its
effect already landed. Per-tenant fairness: a tenant at its
``max_concurrent_runs`` in-flight cap is skipped and eligible tenants drain
in order of their oldest queued job, so one tenant cannot starve the cluster.

ponytail: Postgres SKIP LOCKED queue, ceiling ~10k jobs/min; upgrade path is
fleet-scheduler (13), which owns the org-wide durable admission queue, once
it lands.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text

LEASE_SECONDS = 60
BACKOFF_BASE_SECONDS = 2


async def enqueue(
    conn,
    tenant_id: str,
    kind: str,
    payload: dict,
    idempotency_key: str,
) -> Optional[str]:
    """Insert a job; a replayed idempotency key is a no-op returning None."""
    row = (
        await conn.execute(
            text(
                "insert into wololo_evals.jobs (tenant_id, kind, payload, idempotency_key) "
                "values (:t, :k, cast(:p as jsonb), :ik) "
                "on conflict (idempotency_key) do nothing returning id"
            ),
            {"t": tenant_id, "k": kind, "p": json.dumps(payload), "ik": idempotency_key},
        )
    ).first()
    return str(row.id) if row else None


async def dequeue(conn, worker_id: str) -> Optional[dict]:
    """Lease the next job honoring per-tenant caps + round-robin fairness.

    Runs as ``wololo_evals_worker`` (global view of jobs + tenants only; the
    job's actual work still executes under the job tenant's RLS scope).
    """
    row = (
        await conn.execute(
            text(
                """
                with in_flight as (
                  select tenant_id, count(*) as c
                  from wololo_evals.jobs
                  where status = 'leased'
                  group by tenant_id
                ),
                eligible_tenant as (
                  select j.tenant_id, min(j.run_after) as oldest
                  from wololo_evals.jobs j
                  join wololo_evals.tenants t on t.tenant_id = j.tenant_id
                  left join in_flight f on f.tenant_id = j.tenant_id
                  where j.status = 'queued' and j.run_after <= now()
                    and coalesce(f.c, 0) < t.max_concurrent_runs
                  group by j.tenant_id
                  order by oldest asc
                  limit 1
                )
                select j.id, j.tenant_id, j.kind, j.payload, j.attempts, j.max_attempts,
                       j.idempotency_key
                from wololo_evals.jobs j
                where j.tenant_id = (select tenant_id from eligible_tenant)
                  and j.status = 'queued' and j.run_after <= now()
                order by j.run_after asc
                for update skip locked
                limit 1
                """
            )
        )
    ).first()
    if row is None:
        return None
    await conn.execute(
        text(
            "update wololo_evals.jobs set status = 'leased', leased_by = :w, "
            "attempts = attempts + 1, "
            f"lease_expires_at = now() + interval '{LEASE_SECONDS} seconds' "
            "where id = :id"
        ),
        {"w": worker_id, "id": row.id},
    )
    payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "kind": row.kind,
        "payload": payload,
        "attempts": row.attempts + 1,
        "max_attempts": row.max_attempts,
        "idempotency_key": row.idempotency_key,
    }


async def heartbeat(conn, job_id: str, worker_id: str) -> bool:
    result = await conn.execute(
        text(
            "update wololo_evals.jobs "
            f"set lease_expires_at = now() + interval '{LEASE_SECONDS} seconds' "
            "where id = :id and leased_by = :w and status = 'leased'"
        ),
        {"id": job_id, "w": worker_id},
    )
    return result.rowcount == 1


async def complete(conn, job_id: str) -> None:
    await conn.execute(
        text("update wololo_evals.jobs set status = 'done' where id = :id"),
        {"id": job_id},
    )


async def fail(conn, job_id: str) -> str:
    """Failed attempt: exponential backoff requeue, or dead past max_attempts."""
    row = (
        await conn.execute(
            text(
                "select attempts, max_attempts from wololo_evals.jobs where id = :id "
                "for update"
            ),
            {"id": job_id},
        )
    ).first()
    if row is None:
        return "missing"
    if row.attempts >= row.max_attempts:
        await conn.execute(
            text("update wololo_evals.jobs set status = 'dead' where id = :id"),
            {"id": job_id},
        )
        return "dead"
    backoff = BACKOFF_BASE_SECONDS ** row.attempts
    await conn.execute(
        text(
            "update wololo_evals.jobs set status = 'queued', leased_by = null, "
            "lease_expires_at = null, "
            "run_after = now() + make_interval(secs => :backoff) "
            "where id = :id"
        ),
        {"id": job_id, "backoff": backoff},
    )
    return "requeued"


async def reap_expired_leases(conn) -> int:
    """Expired leases return to queued (attempts already counted at lease
    time) or die past max_attempts. Crash-safety: a worker that vanished
    mid-job loses the lease, never the job."""
    dead = await conn.execute(
        text(
            "update wololo_evals.jobs set status = 'dead' "
            "where status = 'leased' and lease_expires_at < now() "
            "and attempts >= max_attempts"
        )
    )
    requeued = await conn.execute(
        text(
            "update wololo_evals.jobs set status = 'queued', leased_by = null, "
            "lease_expires_at = null "
            "where status = 'leased' and lease_expires_at < now()"
        )
    )
    return dead.rowcount + requeued.rowcount
