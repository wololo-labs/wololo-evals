"""Payload envelope encryption (SPEC S10) + the forward-only status trigger."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from wololo_evals.crypto import EnvelopeCipher, PayloadIntegrityError
from wololo_evals.db import tenant_txn

H1 = "c" * 64
H2 = "d" * 64


def test_envelope_roundtrip(cipher: EnvelopeCipher):
    payload = {"skill_md": "# hello", "files": {"a.py": "print(1)"}}
    ct, key_id, sha = cipher.encrypt_payload("meridian", payload)
    assert key_id == "test-master-1"
    assert ct[:12] != ct[12:24]  # nonce present
    assert cipher.decrypt_payload("meridian", ct, sha) == payload


def test_envelope_tenant_bound(cipher: EnvelopeCipher):
    ct, _, sha = cipher.encrypt_payload("meridian", {"x": 1})
    # AAD binds the tenant: replaying a Meridian ciphertext under Northwind
    # fails cryptographically, not just by convention.
    with pytest.raises(Exception):
        cipher.decrypt_payload("northwind", ct, sha)


def test_envelope_integrity_check(cipher: EnvelopeCipher):
    ct, _, _ = cipher.encrypt_payload("meridian", {"x": 1})
    with pytest.raises(PayloadIntegrityError):
        cipher.decrypt_payload("meridian", ct, "0" * 64)


def test_bad_master_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        EnvelopeCipher("aabb", "k", str(tmp_path / "r.json"))


async def _mk_run(conn, status="queued") -> str:
    cand = (
        await conn.execute(
            text(
                "insert into wololo_evals.candidates "
                "(tenant_id, candidate_type, candidate_ref, proposed_by, baseline_ref, "
                " payload_ciphertext, payload_key_id, payload_sha256) "
                "values ('meridian', 'skill', 'r', 'p', 'b', '\\x00'::bytea, 'k', :sha) "
                "returning id"
            ),
            {"sha": "2" * 64},
        )
    ).scalar_one()
    return (
        await conn.execute(
            text(
                "insert into wololo_evals.runs "
                "(tenant_id, candidate_id, eval_suite, status, suite_version, rubric_version, n_trials) "
                "values ('meridian', :c, 'skill-review', :st, :s, :r, 5) returning id"
            ),
            {"c": cand, "st": status, "s": H1, "r": H2},
        )
    ).scalar_one()


async def test_status_forward_only(app_engine):
    async with tenant_txn(app_engine, "meridian") as conn:
        run = await _mk_run(conn)
        await conn.execute(
            text("update wololo_evals.runs set status = 'running' where id = :r"),
            {"r": run},
        )
        await conn.execute(
            text("update wololo_evals.runs set status = 'passed' where id = :r"),
            {"r": run},
        )
        with pytest.raises(Exception) as exc:
            await conn.execute(
                text("update wololo_evals.runs set status = 'running' where id = :r"),
                {"r": run},
            )
        assert "cannot regress" in str(exc.value)


async def test_n_trials_floor_enforced_in_ddl(app_engine):
    async with tenant_txn(app_engine, "meridian") as conn:
        with pytest.raises(Exception) as exc:
            await conn.execute(
                text(
                    "update wololo_evals.tenants set n_trials = 1 "
                    "where tenant_id = 'meridian'"
                )
            )
        assert "n_trials" in str(exc.value)


def _mint_keys(path, names, barrier):  # noqa: D103
    from wololo_evals.crypto import EnvelopeCipher

    cipher = EnvelopeCipher("aa" * 32, "test-master-1", path)
    barrier.wait()
    for n in names:
        cipher.encrypt_payload(n, {"x": 1})


def test_concurrent_processes_do_not_lose_each_others_tenant_keys(tmp_path):
    # The keyring lock was a threading.Lock, which serialises one process.
    # Two service processes both loaded the old ring and the last writer won,
    # leaving ciphertext whose wrapped key no longer existed.
    import multiprocessing as mp

    from wololo_evals.crypto import EnvelopeCipher

    ctx = mp.get_context("fork")
    path = str(tmp_path / "keyring.json")
    barrier = ctx.Barrier(6)
    groups = [[f"t{p}-{i}" for i in range(8)] for p in range(6)]
    procs = [ctx.Process(target=_mint_keys, args=(path, g, barrier)) for g in groups]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    import json

    ring = json.loads(open(path).read())
    assert sorted(ring) == sorted(n for g in groups for n in g)
