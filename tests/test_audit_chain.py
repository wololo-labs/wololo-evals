"""Invariant 2: per-tenant append-only audit hash chains.

Three proofs the GOAL mandates: (1) recompute a tenant's chain across real
actions and assert it unbroken; (2) tamper with one row's ``resource`` and
assert detection; (3) two tenants' chains are independent.
"""
from __future__ import annotations

from sqlalchemy import text

from wololo_evals.audit import append_audit_event, recompute_chain
from wololo_evals.db import tenant_txn


async def test_chain_recompute_intact(app_engine):
    async with tenant_txn(app_engine, "meridian") as conn:
        for i in range(5):
            await append_audit_event(
                conn, "meridian", "tester", "candidate-submitted", f"cand-{i}"
            )
    async with tenant_txn(app_engine, "meridian") as conn:
        intact, detail = await recompute_chain(conn, "meridian")
        assert intact, detail


async def test_tamper_detected(owner_engine, app_engine):
    async with tenant_txn(app_engine, "meridian") as conn:
        for i in range(3):
            await append_audit_event(
                conn, "meridian", "tester", "run-started", f"run-t-{i}"
            )
    # Tamper as the table owner (RLS cannot stop a DBA; the hash chain must).
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "update wololo_evals.audit_events set resource = 'run-FORGED' "
                "where tenant_id = 'meridian' and resource = 'run-t-1'"
            )
        )
    async with tenant_txn(app_engine, "meridian") as conn:
        intact, detail = await recompute_chain(conn, "meridian")
        assert not intact
        assert "hash mismatch" in detail
    # Repair for later tests (owner path, test-only).
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "update wololo_evals.audit_events set resource = 'run-t-1' "
                "where tenant_id = 'meridian' and resource = 'run-FORGED'"
            )
        )


async def test_chains_independent_across_tenants(app_engine):
    async with tenant_txn(app_engine, "meridian") as conn:
        await append_audit_event(conn, "meridian", "m-actor", "run-completed", "run-m")
    async with tenant_txn(app_engine, "northwind") as conn:
        await append_audit_event(conn, "northwind", "n-actor", "run-completed", "run-n")

    async with tenant_txn(app_engine, "meridian") as conn:
        m_ok, m_detail = await recompute_chain(conn, "meridian")
    async with tenant_txn(app_engine, "northwind") as conn:
        n_ok, n_detail = await recompute_chain(conn, "northwind")
    assert m_ok and n_ok, (m_detail, n_detail)

    # Sequences are per-tenant monotonic from 0, not shared.
    async with tenant_txn(app_engine, "northwind") as conn:
        seqs = (
            await conn.execute(
                text(
                    "select seq from wololo_evals.audit_events "
                    "where tenant_id = 'northwind' order by seq"
                )
            )
        ).scalars().all()
        assert seqs == list(range(len(seqs)))


async def test_unknown_action_rejected(app_engine):
    import pytest

    async with tenant_txn(app_engine, "meridian") as conn:
        with pytest.raises(ValueError):
            await append_audit_event(conn, "meridian", "x", "made-up-action", "r")
