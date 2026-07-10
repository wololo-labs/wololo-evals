"""Durable queue invariants on real Postgres (SPEC S11): idempotency,
per-tenant concurrency caps with round-robin fairness, backoff, dead-letter,
lease reaping."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from wololo_evals.jobs import queue


async def _clear_jobs(owner_engine):
    async with owner_engine.begin() as conn:
        await conn.execute(text("delete from wololo_evals.jobs"))


async def _set_cap(owner_engine, tenant_id: str, cap: int):
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "update wololo_evals.tenants set max_concurrent_runs = :c "
                "where tenant_id = :t"
            ),
            {"c": cap, "t": tenant_id},
        )


def _key() -> str:
    return f"eval-run:{uuid.uuid4()}"


async def test_idempotency_same_key_twice_one_effect(owner_engine, worker_engine):
    await _clear_jobs(owner_engine)
    key = _key()
    async with worker_engine.begin() as conn:
        first = await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "r1"}, key)
        second = await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "r1"}, key)
    assert first is not None
    assert second is None
    async with owner_engine.begin() as conn:
        count = (
            await conn.execute(
                text("select count(*) from wololo_evals.jobs where idempotency_key = :k"),
                {"k": key},
            )
        ).scalar_one()
    assert count == 1


async def test_per_tenant_cap_and_round_robin(owner_engine, worker_engine):
    await _clear_jobs(owner_engine)
    await _set_cap(owner_engine, "meridian", 1)
    await _set_cap(owner_engine, "northwind", 2)
    async with worker_engine.begin() as conn:
        # meridian enqueues first (oldest), northwind second
        await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "m1"}, _key())
        await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "m2"}, _key())
        await queue.enqueue(conn, "northwind", "eval-run", {"run_id": "n1"}, _key())

    async with worker_engine.begin() as conn:
        j1 = await queue.dequeue(conn, "w1")
    assert j1["tenant_id"] == "meridian"  # oldest queued job wins

    # meridian now at its cap of 1 in-flight: fairness must hand northwind the slot
    async with worker_engine.begin() as conn:
        j2 = await queue.dequeue(conn, "w1")
    assert j2["tenant_id"] == "northwind"

    # both capped tenants' remaining jobs wait: meridian at cap, northwind drained
    async with worker_engine.begin() as conn:
        j3 = await queue.dequeue(conn, "w1")
    assert j3 is None

    # completing meridian's job frees its slot
    async with worker_engine.begin() as conn:
        await queue.complete(conn, j1["id"])
        j4 = await queue.dequeue(conn, "w1")
    assert j4 is not None and j4["tenant_id"] == "meridian"
    assert j4["payload"] == {"run_id": "m2"}


async def test_fail_backoff_then_dead(owner_engine, worker_engine):
    await _clear_jobs(owner_engine)
    await _set_cap(owner_engine, "meridian", 5)
    key = _key()
    async with worker_engine.begin() as conn:
        await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "rb"}, key)
        # shrink max_attempts so the test is fast
        await conn.execute(
            text("update wololo_evals.jobs set max_attempts = 2 where idempotency_key = :k"),
            {"k": key},
        )

    async with worker_engine.begin() as conn:
        job = await queue.dequeue(conn, "w1")
        outcome = await queue.fail(conn, job["id"])
    assert outcome == "requeued"
    async with owner_engine.begin() as conn:
        run_after_future = (
            await conn.execute(
                text(
                    "select run_after > now() from wololo_evals.jobs "
                    "where idempotency_key = :k"
                ),
                {"k": key},
            )
        ).scalar_one()
    assert run_after_future  # exponential backoff pushed it out

    # make it eligible again, fail again -> attempts (2) >= max_attempts (2): dead
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("update wololo_evals.jobs set run_after = now() where idempotency_key = :k"),
            {"k": key},
        )
    async with worker_engine.begin() as conn:
        job = await queue.dequeue(conn, "w1")
        outcome = await queue.fail(conn, job["id"])
    assert outcome == "dead"


async def test_expired_lease_reaped(owner_engine, worker_engine):
    await _clear_jobs(owner_engine)
    key = _key()
    async with worker_engine.begin() as conn:
        await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "rl"}, key)
        job = await queue.dequeue(conn, "w-crash")
    assert job is not None
    # simulate the lease expiring (worker vanished mid-job)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "update wololo_evals.jobs set lease_expires_at = now() - interval '1 minute' "
                "where id = :id"
            ),
            {"id": job["id"]},
        )
    async with worker_engine.begin() as conn:
        reaped = await queue.reap_expired_leases(conn)
    assert reaped == 1
    async with worker_engine.begin() as conn:
        again = await queue.dequeue(conn, "w2")
    assert again is not None and again["id"] == job["id"]


async def test_heartbeat_only_extends_own_lease(owner_engine, worker_engine):
    await _clear_jobs(owner_engine)
    async with worker_engine.begin() as conn:
        await queue.enqueue(conn, "meridian", "eval-run", {"run_id": "rh"}, _key())
        job = await queue.dequeue(conn, "w1")
    async with worker_engine.begin() as conn:
        assert await queue.heartbeat(conn, job["id"], "w1") is True
        assert await queue.heartbeat(conn, job["id"], "w2") is False
