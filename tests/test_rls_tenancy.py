"""Invariant 1: tenancy. Cross-tenant leaks MUST fail loudly, rows AND blobs.

Runs against real Postgres RLS as the real ``wololo_evals_app`` role (no
bypassrls, RLS forced). Northwind-scoped reads of Meridian-owned rows return
zero rows; a Northwind-scoped fetch of a Meridian ``.eval`` blob key is denied
at the store layer (the API maps that to 404 + a denied audit row, proven in
the e2e suite). These tests FAIL (never skip) if scoping regresses.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from wololo_evals.blobstore import BlobAccessDenied, FsBlobStore, eval_log_key
from wololo_evals.db import tenant_txn

H1 = "a" * 64
H2 = "b" * 64


async def _seed_meridian_run(app_engine) -> str:
    async with tenant_txn(app_engine, "meridian") as conn:
        cand = (
            await conn.execute(
                text(
                    "insert into wololo_evals.candidates "
                    "(tenant_id, candidate_type, candidate_ref, proposed_by, baseline_ref, "
                    " payload_ciphertext, payload_key_id, payload_sha256) "
                    "values ('meridian', 'skill', 'ref', 'tester', 'base', "
                    "        '\\x00'::bytea, 'k1', :sha) returning id"
                ),
                {"sha": "0" * 64},
            )
        ).scalar_one()
        run = (
            await conn.execute(
                text(
                    "insert into wololo_evals.runs "
                    "(tenant_id, candidate_id, eval_suite, status, suite_version, rubric_version, n_trials) "
                    "values ('meridian', :cand, 'skill-review', 'queued', :s, :r, 5) returning id"
                ),
                {"cand": cand, "s": H1, "r": H2},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "insert into wololo_evals.scores "
                "(tenant_id, run_id, dimension, value, suite_version, rubric_version) "
                "values ('meridian', :run, 'validation_gate', 0.9, :s, :r)"
            ),
            {"run": run, "s": H1, "r": H2},
        )
        return str(run)


async def test_cross_tenant_rows_invisible(app_engine):
    run_id = await _seed_meridian_run(app_engine)

    # Meridian sees its own rows.
    async with tenant_txn(app_engine, "meridian") as conn:
        n = (
            await conn.execute(
                text("select count(*) from wololo_evals.runs where id = :r"),
                {"r": run_id},
            )
        ).scalar_one()
        assert n == 1

    # Northwind sees ZERO rows across every table holding Meridian data.
    async with tenant_txn(app_engine, "northwind") as conn:
        for table, where, params in (
            ("runs", "id = :r", {"r": run_id}),
            ("runs", "tenant_id = 'meridian'", {}),
            ("candidates", "tenant_id = 'meridian'", {}),
            ("scores", "run_id = :r", {"r": run_id}),
            ("trials", "tenant_id = 'meridian'", {}),
            ("leaderboard", "tenant_id = 'meridian'", {}),
            ("canary_gates", "tenant_id = 'meridian'", {}),
            ("jobs", "tenant_id = 'meridian'", {}),
            ("audit_events", "tenant_id = 'meridian'", {}),
        ):
            n = (
                await conn.execute(
                    text(f"select count(*) from wololo_evals.{table} where {where}"),
                    params,
                )
            ).scalar_one()
            assert n == 0, f"NORTHWIND CAN SEE MERIDIAN ROWS IN {table} — tenancy regressed"


async def test_no_tenant_scope_denies_everything(app_engine):
    await _seed_meridian_run(app_engine)
    async with app_engine.begin() as conn:  # no GUC, no JWT: fail closed
        n = (
            await conn.execute(text("select count(*) from wololo_evals.runs"))
        ).scalar_one()
        assert n == 0


async def test_jwt_claim_overrides_guc(app_engine):
    """A JWT claims blob is authoritative; the GUC cannot widen it."""
    await _seed_meridian_run(app_engine)
    async with app_engine.begin() as conn:
        await conn.execute(
            text("select set_config('app.tenant_id', 'meridian', true)")
        )
        await conn.execute(
            text(
                "select set_config('request.jwt.claims', '{\"tenant_id\": \"northwind\"}', true)"
            )
        )
        n = (
            await conn.execute(
                text("select count(*) from wololo_evals.runs where tenant_id = 'meridian'")
            )
        ).scalar_one()
        assert n == 0


async def test_jwt_without_tenant_claim_denies(app_engine):
    """A JWT WITHOUT the tenant claim denies; no GUC fallback (fail closed)."""
    await _seed_meridian_run(app_engine)
    async with app_engine.begin() as conn:
        await conn.execute(
            text("select set_config('app.tenant_id', 'meridian', true)")
        )
        await conn.execute(
            text("select set_config('request.jwt.claims', '{\"sub\": \"x\"}', true)")
        )
        n = (
            await conn.execute(text("select count(*) from wololo_evals.runs"))
        ).scalar_one()
        assert n == 0


async def test_cross_tenant_insert_rejected(app_engine):
    async with tenant_txn(app_engine, "northwind") as conn:
        with pytest.raises(Exception) as exc:
            await conn.execute(
                text(
                    "insert into wololo_evals.candidates "
                    "(tenant_id, candidate_type, candidate_ref, proposed_by, baseline_ref, "
                    " payload_ciphertext, payload_key_id, payload_sha256) "
                    "values ('meridian', 'skill', 'evil', 'northwind-actor', 'base', "
                    "        '\\x00'::bytea, 'k1', :sha)"
                ),
                {"sha": "1" * 64},
            )
        assert "row-level security" in str(exc.value)


def test_cross_tenant_blob_fetch_denied(blobstore: FsBlobStore):
    key = eval_log_key("meridian", str(uuid.uuid4()), "treatment", 0)
    blobstore.put("meridian", key, b"meridian eval log")
    # Northwind resolved identity fetching a Meridian key: denied at the store
    # layer before any filesystem/S3 access.
    with pytest.raises(BlobAccessDenied):
        blobstore.get("northwind", key)
    # And a forged prefix cannot help: the key itself carries the tenant.
    with pytest.raises(BlobAccessDenied):
        blobstore.get("northwind", key.replace("evals/meridian/", "evals/northwind/../meridian/"))


def test_blob_put_requires_matching_prefix(blobstore: FsBlobStore):
    with pytest.raises(BlobAccessDenied):
        blobstore.put("northwind", "evals/meridian/run/x.eval", b"forged")
