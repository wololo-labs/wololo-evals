"""Canary-gate lifecycle on real RLS Postgres + the real deploy stub (SPEC
S5.5): open-on-passed only (409 otherwise), two-consecutive-window debounce,
live composite computed from RAW windows, and exactly-once idempotent rollback
that survives a worker crash between rollback intent and confirmation.

These cover adversarial-verify attacks (b) queued-run -> 409 and (c) the
two-window debounce, plus Meridian proof #3 (watching -> regressed ->
rolled-back with the two audit rows and a single Idempotency-Key'd rollback).
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from wololo_evals import canary, versioning
from wololo_evals.crypto import EnvelopeCipher
from wololo_evals.db import tenant_txn
from wololo_evals.jobs.worker import run_once
from wololo_evals.stubs.deploy_stub import create_deploy_stub
from wololo_evals.suites import base as suites_base

TENANT = "meridian"
# The run's frozen eval-dimension composite (correctness/turns/approach). This
# is the ~0.89-scale number a real passed task-uplift run produces; it is NOT
# the canary baseline (a disjoint operational-metric space), and the gate must
# never copy it into baseline_composite (that was the false-regression bug).
RUN_COMPOSITE = 0.89
# task-uplift canary_weights: task_success_rate 0.6, error_rate -0.25,
# tokens_per_task -0.15. The baseline is frozen from the FIRST telemetry window
# (the stable/pre-shift cohort) so it shares this operational space with every
# live window it is compared against.
HEALTHY = {"task_success_rate": 0.95, "error_rate": 0.05, "tokens_per_task": 0.10}
REGRESSING = {"task_success_rate": 0.50, "error_rate": 0.30, "tokens_per_task": 0.40}
HEALTHY_COMPOSITE = 0.6 * 0.95 - 0.25 * 0.05 - 0.15 * 0.10  # == 0.5425


@pytest.fixture()
def cipher(tmp_path):
    return EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "keyring.json"))


@pytest.fixture()
async def deploy_client():
    app = create_deploy_stub()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://deploy") as c:
        c._stub_app = app  # test reaches the in-memory rollback log through here
        yield c


async def _passed_run(engine, cipher, *, status="passed", low_confidence=False) -> tuple[str, str]:
    """Insert a candidate + a task-uplift run in the given status. Returns
    (candidate_id, run_id)."""
    ct, kid, sha = cipher.encrypt_payload(TENANT, {"SKILL.md": "# skill"})
    cid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    sv = versioning.suite_version(suites_base.suite_dir("task-uplift"))
    rv = versioning.rubric_version(suites_base.rubric_path("task-uplift"))
    vstats = {"mean": 8.0, "stddev": 1.0, "ci_low": 6.0, "ci_high": 10.0,
              "wins": 5, "losses": 0, "n": 5, "low_confidence": low_confidence}
    async with tenant_txn(engine, TENANT) as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.candidates "
                "(id, tenant_id, candidate_type, candidate_ref, proposed_by, "
                " baseline_ref, payload_ciphertext, payload_key_id, payload_sha256) "
                "values (:id,:t,'skill','s@2','actor','s@1',:ct,:kid,:sha)"
            ),
            {"id": cid, "t": TENANT, "ct": ct, "kid": kid, "sha": sha},
        )
        await conn.execute(
            text(
                "insert into wololo_evals.runs "
                "(id, tenant_id, candidate_id, eval_suite, status, suite_version, "
                " rubric_version, n_trials, composite_score, verdict_stats) "
                "values (:id,:t,:c,'task-uplift',:st,:sv,:rv,5,:cs,cast(:vs as jsonb))"
            ),
            {"id": rid, "t": TENANT, "c": cid, "st": status, "sv": sv, "rv": rv,
             "cs": RUN_COMPOSITE, "vs": json.dumps(vstats)},
        )
    return cid, rid


async def _open_gate(engine, cid, rid, deploy_ref) -> str:
    async with tenant_txn(engine, TENANT) as conn:
        gate = await canary.open_gate(conn, TENANT, "actor", cid, rid, deploy_ref)
    return gate["id"]


async def _send_window(engine, gate_id, window_id, metrics):
    # observed_at ascends with the digits in window_id (w0 -> :00, w1 -> :01,
    # ...) so "last two windows by observed_at" is deterministic even though all
    # windows share a session second — the baseline window (w0) stays oldest.
    secs = int("".join(ch for ch in window_id if ch.isdigit()) or "0")
    observed_at = f"2026-07-10T00:00:{secs:02d}Z"
    async with tenant_txn(engine, TENANT) as conn:
        return await canary.ingest_telemetry(
            conn, TENANT, gate_id, window_id, metrics, 100, observed_at
        )


async def _gate_row(engine, gate_id):
    async with tenant_txn(engine, TENANT) as conn:
        return (
            await conn.execute(
                text("select * from wololo_evals.canary_gates where id = :id"),
                {"id": gate_id},
            )
        ).first()


# --- open rules (adversarial b) ---------------------------------------------
async def test_open_on_queued_run_is_refused(app_engine, cipher):
    cid, rid = await _passed_run(app_engine, cipher, status="queued")
    with pytest.raises(canary.GateError):
        await _open_gate(app_engine, cid, rid, "dep-queued")


async def test_open_on_low_confidence_run_is_refused(app_engine, cipher):
    cid, rid = await _passed_run(app_engine, cipher, low_confidence=True)
    with pytest.raises(canary.GateError):
        await _open_gate(app_engine, cid, rid, "dep-lowconf")


async def test_open_on_a_smoke_run_is_refused(app_engine, cipher):
    # Migration 002 says a smoke run is never verdict-bearing and the gate must
    # reject it; open_gate never looked at the flag.
    cid, rid = await _passed_run(app_engine, cipher)
    async with tenant_txn(app_engine, TENANT) as conn:
        await conn.execute(
            text("update wololo_evals.runs set smoke = true where id = :id"), {"id": rid}
        )
    with pytest.raises(canary.GateError, match="smoke"):
        await _open_gate(app_engine, cid, rid, "dep-smoke")


async def test_open_with_a_candidate_that_is_not_the_runs_is_refused(app_engine, cipher):
    # The gate recorded whichever candidate the caller named, so a passed run
    # of one candidate could put another under watch.
    _, rid = await _passed_run(app_engine, cipher)
    other_cid, _ = await _passed_run(app_engine, cipher)
    with pytest.raises(canary.GateError, match="candidate"):
        await _open_gate(app_engine, other_cid, rid, "dep-mismatch")


async def test_open_on_passed_run_watches(app_engine, cipher):
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-ok")
    row = await _gate_row(app_engine, gid)
    assert row.gate_status == "watching"
    # No baseline is copied from the run's eval composite; it stays NULL until
    # the first telemetry window freezes it in the operational metric space.
    assert row.baseline_composite is None


async def test_first_window_freezes_baseline(app_engine, cipher):
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-baseline")
    await _send_window(app_engine, gid, "w0", HEALTHY)
    row = await _gate_row(app_engine, gid)
    assert row.baseline_composite == pytest.approx(HEALTHY_COMPOSITE)
    # A second, different window never re-baselines (first-writer wins).
    await _send_window(app_engine, gid, "w1", REGRESSING)
    row = await _gate_row(app_engine, gid)
    assert row.baseline_composite == pytest.approx(HEALTHY_COMPOSITE)


# --- live composite from RAW metrics ----------------------------------------
def test_live_composite_is_weighted_sum_not_trusted():
    rubric = suites_base.load_rubric("task-uplift")
    live = canary.live_composite(rubric, HEALTHY)
    expected = 0.6 * 0.95 - 0.25 * 0.05 - 0.15 * 0.10
    assert live == pytest.approx(expected)


async def test_telemetry_missing_metric_raises(app_engine, cipher):
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-miss")
    with pytest.raises(ValueError):
        await _send_window(app_engine, gid, "w1", {"task_success_rate": 0.9})


# --- two-window debounce (adversarial c) ------------------------------------
async def test_one_regressing_window_stays_watching(app_engine, cipher, deploy_client):
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-1win")
    await _send_window(app_engine, gid, "w0", HEALTHY)  # baseline (pre-shift)
    await _send_window(app_engine, gid, "w1", REGRESSING)
    status = await canary.run_canary_poll(app_engine, TENANT, gid, deploy_client)
    assert status == "watching"
    assert deploy_client._stub_app.state.log.calls == []  # no rollback on one window


async def test_two_regressing_windows_roll_back_once(app_engine, cipher, deploy_client):
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-2win")

    await _send_window(app_engine, gid, "w0", HEALTHY)  # baseline (pre-shift)
    # window 1: regressing but alone (paired with baseline) -> still watching
    await _send_window(app_engine, gid, "w1", REGRESSING)
    assert await canary.run_canary_poll(app_engine, TENANT, gid, deploy_client) == "watching"
    # window 2: two consecutive regressing windows -> rollback fires
    await _send_window(app_engine, gid, "w2", REGRESSING)
    status = await canary.run_canary_poll(app_engine, TENANT, gid, deploy_client)
    assert status == "rolled-back"

    row = await _gate_row(app_engine, gid)
    assert row.gate_status == "rolled-back"
    assert row.rollback_key == "dep-2win:" + rid
    assert row.rollback_intent_at is not None
    assert row.rollback_confirmed_at is not None

    log = deploy_client._stub_app.state.log
    assert len(log.calls) == 1
    assert log.calls[0]["idempotency_key"] == "dep-2win:" + rid

    async with tenant_txn(app_engine, TENANT) as conn:
        actions = (
            await conn.execute(
                text(
                    "select action from wololo_evals.audit_events "
                    "where resource = 'dep-2win' order by seq"
                )
            )
        ).scalars().all()
    assert actions == ["canary-opened", "canary-regression-detected", "canary-rollback-fired"]


# --- exactly-once across a crash (adversarial e) ----------------------------
async def test_rollback_idempotent_across_worker_crash(app_engine, cipher, deploy_client):
    """Persist the rollback intent (txn 1), then simulate a crash before the
    HTTP confirm and a restart that re-polls: the same key is re-sent, so the
    deploy stub records exactly one rollback."""
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-crash")
    await _send_window(app_engine, gid, "w0", HEALTHY)  # baseline (pre-shift)
    await _send_window(app_engine, gid, "w1", REGRESSING)
    await _send_window(app_engine, gid, "w2", REGRESSING)

    # txn 1 only: intent persisted, gate 'regressed', NO http call yet (crash).
    async with tenant_txn(app_engine, TENANT) as conn:
        claim = await canary.detect_and_claim(conn, TENANT, "actor", gid)
    assert claim is not None
    row = await _gate_row(app_engine, gid)
    assert row.gate_status == "regressed" and row.rollback_confirmed_at is None

    # restart: a fresh poll picks up the pending intent and drives it home.
    assert await canary.run_canary_poll(app_engine, TENANT, gid, deploy_client) == "rolled-back"
    # a second redelivered poll must be a no-op (gate already rolled-back).
    assert await canary.run_canary_poll(app_engine, TENANT, gid, deploy_client) == "rolled-back"

    log = deploy_client._stub_app.state.log
    assert len(log.calls) == 1  # exactly one real rollback across crash + retries


# --- durable queue path: worker services a canary-poll job ------------------
async def test_worker_services_canary_poll_job(
    app_engine, worker_engine, owner_engine, cipher, deploy_client
):
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-queue")
    await _send_window(app_engine, gid, "w0", HEALTHY)  # baseline (pre-shift)
    await _send_window(app_engine, gid, "w1", REGRESSING)
    await _send_window(app_engine, gid, "w2", REGRESSING)
    from wololo_evals.jobs import queue

    # The jobs table is shared session state; other suites leave unprocessed
    # eval-run jobs queued. Clear them so run_once leases the canary-poll job
    # under test (superuser bypasses RLS for this cross-tenant cleanup).
    async with owner_engine.begin() as conn:
        await conn.execute(text("delete from wololo_evals.jobs where status = 'queued'"))
    async with tenant_txn(app_engine, TENANT) as conn:
        await queue.enqueue(
            conn, TENANT, "canary-poll", {"gate_id": gid, "window_id": "w2"},
            f"canary-poll:{gid}:w2",
        )

    def _no_judge(system, user):  # not exercised on a canary-poll job
        raise AssertionError("judge must not be called for canary-poll")

    job = await run_once(
        worker_engine, cipher=cipher, call_model=_no_judge,
        worker_id="w-canary", deploy_client=deploy_client,
    )
    assert job is not None and job["kind"] == "canary-poll"
    row = await _gate_row(app_engine, gid)
    assert row.gate_status == "rolled-back"
    assert len(deploy_client._stub_app.state.log.calls) == 1


# --- HTTP routes ------------------------------------------------------------
@pytest.fixture()
def http_client(app_engine, tmp_path):
    from wololo_evals.api import AppDeps, create_app
    from wololo_evals.auth import Verifier
    from wololo_evals.blobstore import FsBlobStore
    from support import JWKS_PATH

    deps = AppDeps(
        engine=app_engine,
        verifier=Verifier(JWKS_PATH, "wololo-deploy"),
        cipher=EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "kr.json")),
        blobstore=FsBlobStore(str(tmp_path / "blobs")),
    )
    transport = httpx.ASGITransport(app=create_app(deps))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_http_open_gate_on_queued_run_is_409(http_client, app_engine, cipher):
    from support import mint_token

    cid, rid = await _passed_run(app_engine, cipher, status="queued")
    token = mint_token(TENANT, ["wololo-evals:gatekeeper"])
    async with http_client as c:
        r = await c.post(
            "/api/v1/canary-gates",
            json={"candidate_id": cid, "run_id": rid, "deploy_ref": "dep-http-q"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 409, r.text


async def test_http_telemetry_with_composite_is_422(http_client, app_engine, cipher):
    from support import mint_token

    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-http-422")
    token = mint_token(TENANT, ["wololo-evals:gatekeeper"])
    async with http_client as c:
        r = await c.post(
            f"/api/v1/canary-gates/{gid}/telemetry",
            json={"window_id": "w1", "sample_count": 10, "observed_at": "2026-07-10T00:00:00Z",
                  "composite": 0.9, "metrics": HEALTHY},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 422, r.text


async def test_http_open_watch_and_verdict(http_client, app_engine, cipher):
    from support import mint_token

    cid, rid = await _passed_run(app_engine, cipher)
    token = mint_token(TENANT, ["wololo-evals:gatekeeper"])
    async with http_client as c:
        opened = await c.post(
            "/api/v1/canary-gates",
            json={"candidate_id": cid, "run_id": rid, "deploy_ref": "dep-http-ok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert opened.status_code == 201, opened.text
        gid = opened.json()["id"]
        tele = await c.post(
            f"/api/v1/canary-gates/{gid}/telemetry",
            json={"window_id": "w1", "sample_count": 10,
                  "observed_at": "2026-07-10T00:00:00Z", "metrics": HEALTHY},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert tele.status_code == 202, tele.text
        assert tele.json()["live_composite"] == pytest.approx(HEALTHY_COMPOSITE)
        verdict = await c.get(
            f"/api/v1/canary-gates/{gid}/verdict",
            headers={"Authorization": f"Bearer {token}"},
        )
    body = verdict.json()
    assert body["gate_status"] == "watching"
    # baseline is the first window's live composite, in the same space as live.
    assert body["baseline_composite"] == pytest.approx(HEALTHY_COMPOSITE)
    assert body["live_composite"] == pytest.approx(HEALTHY_COMPOSITE)
    assert body["windows_observed"] == 1


# --- the false-regression regression: eval composite != canary baseline ------
async def test_real_run_composite_never_poisons_canary_baseline(
    app_engine, cipher, deploy_client
):
    """A gate opened on a REAL passed task-uplift run (composite_score ~0.89,
    frozen by the worker, NOT hand-seeded to a canary-scale value) must stay
    'watching' under healthy live traffic. Copying the eval composite into
    baseline_composite (the old bug) would make is_regression(0.54, 0.89, 10%)
    fire and roll back a perfectly healthy canary after two windows."""
    from wololo_evals.jobs.worker import execute_run

    from test_harness_uplift import FakeSandbox, _judge, _make_uplift_run

    rid = await _make_uplift_run(app_engine, cipher, "claude-code")
    fake = FakeSandbox()
    async with tenant_txn(app_engine, TENANT) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_judge, k=5,
            exec_factory=lambda: fake.exec,
        )
    assert out["status"] == "passed"
    # eval-dimension composite, a disjoint space from canary_weights:
    assert out["composite_score"] == pytest.approx(0.89, abs=1e-6)

    async with tenant_txn(app_engine, TENANT) as conn:
        cid = (
            await conn.execute(
                text("select candidate_id from wololo_evals.runs where id = :r"),
                {"r": rid},
            )
        ).scalar_one()
    gid = await _open_gate(app_engine, str(cid), rid, "dep-real")
    row = await _gate_row(app_engine, gid)
    assert row.baseline_composite is None  # never the 0.89 eval composite

    # Healthy traffic: first window freezes a ~0.54 baseline, second confirms it
    # stays watching — no rollback despite the run's 0.89 eval composite.
    await _send_window(app_engine, gid, "w0", HEALTHY)
    await _send_window(app_engine, gid, "w1", HEALTHY)
    assert await canary.run_canary_poll(app_engine, TENANT, gid, deploy_client) == "watching"
    row = await _gate_row(app_engine, gid)
    assert row.baseline_composite == pytest.approx(HEALTHY_COMPOSITE)
    assert deploy_client._stub_app.state.log.calls == []  # NO false rollback


# --- transient non-2xx rollback must retry with the same key (adversarial) ---
async def test_rollback_retries_on_transient_5xx(app_engine, cipher):
    """A transient 5xx on the rollback POST must not strand the gate at
    'regressed': the poll raises so the durable queue retries, and the retry
    re-sends the SAME Idempotency-Key until wololo-deploy confirms (SPEC S5.5
    exactly-once, not at-most-once)."""
    cid, rid = await _passed_run(app_engine, cipher)
    gid = await _open_gate(app_engine, cid, rid, "dep-503")
    await _send_window(app_engine, gid, "w0", HEALTHY)  # baseline (pre-shift)
    await _send_window(app_engine, gid, "w1", REGRESSING)
    await _send_window(app_engine, gid, "w2", REGRESSING)

    seen_keys: list = []
    state = {"n": 0}

    def handler(request):
        seen_keys.append(request.headers.get("Idempotency-Key"))
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(200, json={"rolled_back": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://deploy"
    )
    async with client:
        with pytest.raises(canary.RollbackNotConfirmed):
            await canary.run_canary_poll(app_engine, TENANT, gid, client)
        row = await _gate_row(app_engine, gid)
        assert row.gate_status == "regressed"
        assert row.rollback_intent_at is not None
        assert row.rollback_confirmed_at is None  # never confirmed on the 503

        # retry: deploy now healthy -> rolled-back with the SAME key
        assert await canary.run_canary_poll(app_engine, TENANT, gid, client) == "rolled-back"

    assert state["n"] == 2
    assert len(set(seen_keys)) == 1  # same Idempotency-Key across both attempts
    assert seen_keys[0] == f"dep-503:{rid}"
    row = await _gate_row(app_engine, gid)
    assert row.gate_status == "rolled-back"
    assert row.rollback_confirmed_at is not None
