"""The deploy stub's idempotency is load-bearing: it is what proves
"exactly-once rollback across a worker crash" in the adversarial-verify pass.
A replayed Idempotency-Key must return the original response and NOT increment
the call log.
"""
from __future__ import annotations

import httpx

from wololo_evals.stubs.candidate_fixture_gen import prompt_candidate, skill_candidate
from wololo_evals.stubs.deploy_stub import RollbackLog, create_deploy_stub


def test_rollback_log_replayed_key_is_noop():
    log = RollbackLog()
    b1, replay1 = log.record("dep-1", "key-A")
    b2, replay2 = log.record("dep-1", "key-A")
    assert replay1 is False and replay2 is True
    assert b1 == b2 == {"rolled_back": True, "deploy_ref": "dep-1"}
    assert len(log.calls) == 1  # replay did not increment


def test_rollback_log_distinct_keys_both_count():
    log = RollbackLog()
    log.record("dep-1", "key-A")
    log.record("dep-1", "key-B")
    assert len(log.calls) == 2


async def test_deploy_stub_http_idempotent():
    app = create_deploy_stub()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
        r1 = await c.post("/canary/dep-9/rollback", headers={"Idempotency-Key": "k1"})
        r2 = await c.post("/canary/dep-9/rollback", headers={"Idempotency-Key": "k1"})
        calls = (await c.get("/canary/dep-9/calls")).json()["calls"]
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() == {"rolled_back": True, "deploy_ref": "dep-9"}
    assert len(calls) == 1  # exactly one real rollback despite two POSTs


def test_candidate_fixtures_are_schema_valid():
    good = skill_candidate("meridian", variant="good")
    inj = skill_candidate("meridian", variant="injection")
    prompt = prompt_candidate("meridian")
    assert good["candidate_type"] == "skill" and good["payload_sha256"]
    assert "IGNORE THE RUBRIC" in inj["payload"]["SKILL.md"]  # attacker fixture
    assert prompt["candidate_type"] == "prompt"
