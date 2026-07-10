"""Canary-gate lifecycle (SPEC S5.5), wired to ``wololo-deploy``.

The gate watches a candidate that already scored ``passed`` in a real eval run
while ``wololo-deploy`` shifts live traffic to it. ``wololo-deploy`` posts RAW
metric windows; wololo-evals computes the live composite ITSELF from the same
pinned rubric's ``canary_weights`` (a self-reported composite is never
accepted). A regression is the live composite dropping more than the tenant's
``regression_threshold_pct`` below the frozen baseline for TWO consecutive
windows (never one noisy sample). On regression the gate fires
``wololo-deploy``'s rollback EXACTLY ONCE across worker restarts:

    txn 1: persist rollback_key + rollback_intent_at, flip to 'regressed'
    ------ (commit; unique rollback_key makes a second claim impossible) ------
    HTTP : POST {deploy_ref}/rollback  with  Idempotency-Key: {rollback_key}
    txn 2: on a confirmed 2xx only, flip to 'rolled-back'

A crash between txn 1 and txn 2 leaves the gate 'regressed' with intent set but
unconfirmed; a retry re-sends the SAME idempotency key, so the deploy side (and
its stub) dedupes to a single real rollback.

The three audit actions (``canary-opened``, ``canary-regression-detected``,
``canary-rollback-fired``) land on the per-tenant hash chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from .audit import append_audit_event
from .db import tenant_txn
from .suites import base as suites_base


class GateError(Exception):
    """Gate open rejected (maps to 409): the referenced run never legally
    passed, or its verdict is low-confidence (SPEC S7)."""


def live_composite(rubric: dict, metrics: dict) -> float:
    """Weighted sum of the raw metric window under the rubric's canary weights.

    Every metric named in ``canary_weights`` must be present in the window;
    a missing metric is a malformed telemetry post, never a defaulted zero.

    ponytail: assumes rate-style metrics already normalized to 0..1 (the same
    scale as the frozen baseline composite) so the percentage-drop regression
    test is meaningful; wire a per-metric normalizer here if wololo-deploy ever
    posts unbounded raw counts.
    """
    weights = rubric.get("canary_weights")
    if not weights:
        raise GateError(f"suite rubric {rubric.get('suite')!r} defines no canary_weights")
    total = 0.0
    for metric, weight in weights.items():
        if metric not in metrics:
            raise ValueError(f"telemetry window missing canary metric {metric!r}")
        total += float(weight) * float(metrics[metric])
    return total


def is_regression(live: float, baseline: float, threshold_pct: float) -> bool:
    """True when ``live`` is more than ``threshold_pct`` below ``baseline``."""
    return live < baseline * (1.0 - threshold_pct / 100.0)


async def open_gate(
    conn, tenant_id: str, actor: str, candidate_id: str, run_id: str, deploy_ref: str
) -> dict:
    """Open a 'watching' gate on a passed, high-confidence run (SPEC S5.5).

    Rejects with :class:`GateError` (409) if the run is not ``passed`` or its
    verdict is ``low_confidence`` — a candidate that never passed its own eval
    is never silently watched.
    """
    run = (
        await conn.execute(
            text(
                "select status, composite_score, rubric_version, verdict_stats "
                "from wololo_evals.runs where id = :id"
            ),
            {"id": run_id},
        )
    ).first()
    if run is None:
        raise GateError("no such run")
    if run.status != "passed":
        raise GateError(f"run status {run.status!r} is not 'passed'; gate refused")
    if run.composite_score is None:
        raise GateError("run has no frozen composite")
    stats = run.verdict_stats
    if isinstance(stats, str):
        import json

        stats = json.loads(stats)
    if stats and stats.get("low_confidence"):
        raise GateError("run verdict is low_confidence; gate refused")

    gate_id = (
        await conn.execute(
            text(
                "insert into wololo_evals.canary_gates "
                "(tenant_id, candidate_id, run_id, deploy_ref, gate_status, "
                " baseline_composite, rubric_version) "
                "values (:t, :c, :r, :d, 'watching', :bc, :rv) returning id"
            ),
            {
                "t": tenant_id,
                "c": candidate_id,
                "r": run_id,
                "d": deploy_ref,
                "bc": run.composite_score,
                "rv": run.rubric_version,
            },
        )
    ).scalar_one()
    await append_audit_event(
        conn, tenant_id, actor, "canary-opened", deploy_ref, decision="allowed"
    )
    return {
        "id": str(gate_id),
        "gate_status": "watching",
        "baseline_composite": run.composite_score,
        "rubric_version": run.rubric_version,
    }


async def _gate_rubric(conn, gate) -> dict:
    """Load the rubric that scored the gated run and verify its version still
    matches the pinned one (never mix rubric versions across a canary, SPEC
    S9). Returns the parsed rubric dict."""
    suite = (
        await conn.execute(
            text("select eval_suite from wololo_evals.runs where id = :id"),
            {"id": gate.run_id},
        )
    ).scalar_one()
    from . import versioning

    if versioning.rubric_version(suites_base.rubric_path(suite)) != gate.rubric_version:
        raise GateError(
            "gated rubric changed on disk since gate open; refusing to rescore "
            "against a different rubric_version"
        )
    return suites_base.load_rubric(suite)


async def ingest_telemetry(
    conn,
    tenant_id: str,
    gate_id: str,
    window_id: str,
    metrics: dict,
    sample_count: int,
    observed_at: str,
) -> dict:
    """Store one RAW telemetry window and its wololo-evals-computed live
    composite. Deduped by ``(gate_id, window_id)``."""
    gate = (
        await conn.execute(
            text(
                "select id, run_id, rubric_version, gate_status "
                "from wololo_evals.canary_gates where id = :id"
            ),
            {"id": gate_id},
        )
    ).first()
    if gate is None:
        raise GateError("no such canary gate")
    rubric = await _gate_rubric(conn, gate)
    live = live_composite(rubric, metrics)
    import json

    await conn.execute(
        text(
            "insert into wololo_evals.canary_telemetry "
            "(tenant_id, gate_id, window_id, metrics, sample_count, live_composite, "
            " observed_at) "
            "values (:t, :g, :w, cast(:m as jsonb), :sc, :lc, cast(:o as timestamptz)) "
            "on conflict (gate_id, window_id) do nothing"
        ),
        {
            "t": tenant_id,
            "g": gate_id,
            "w": window_id,
            "m": json.dumps(metrics),
            "sc": sample_count,
            "lc": live,
            "o": observed_at,
        },
    )
    return {"window_id": window_id, "live_composite": live}


@dataclass
class _RollbackClaim:
    rollback_key: str
    deploy_ref: str


async def detect_and_claim(
    conn, tenant_id: str, actor: str, gate_id: str
) -> Optional[_RollbackClaim]:
    """txn-1 phase: under the tenant transaction, decide whether a rollback
    must fire and (if so) persist the rollback intent atomically.

    Returns a :class:`_RollbackClaim` when the caller should now POST the
    rollback (freshly detected regression OR a pending unconfirmed intent left
    by a prior crash), else ``None``. Idempotent: the ``rollback_key IS NULL``
    guard means a redelivered poll never double-claims.
    """
    gate = (
        await conn.execute(
            text(
                "select id, run_id, deploy_ref, baseline_composite, gate_status, "
                "rollback_key, rollback_intent_at, rollback_confirmed_at "
                "from wololo_evals.canary_gates where id = :id for update"
            ),
            {"id": gate_id},
        )
    ).first()
    if gate is None or gate.gate_status == "rolled-back":
        return None
    # Crash recovery: intent already persisted but never confirmed -> resume
    # firing with the SAME key (exactly-once across restarts).
    if gate.rollback_intent_at is not None and gate.rollback_confirmed_at is None:
        return _RollbackClaim(gate.rollback_key, gate.deploy_ref)
    if gate.gate_status != "watching":
        return None

    windows = (
        await conn.execute(
            text(
                "select live_composite from wololo_evals.canary_telemetry "
                "where gate_id = :g and live_composite is not null "
                "order by observed_at desc limit 2"
            ),
            {"g": gate_id},
        )
    ).all()
    if len(windows) < 2:
        return None  # two-consecutive-window debounce: one window never trips
    threshold = (
        await conn.execute(
            text("select regression_threshold_pct from wololo_evals.tenants where tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar_one()
    if not all(
        is_regression(w.live_composite, gate.baseline_composite, threshold)
        for w in windows
    ):
        return None

    rollback_key = f"{gate.deploy_ref}:{gate.run_id}"
    reason = (
        f"live composite below baseline {gate.baseline_composite:.4f} by more than "
        f"{threshold:.1f}% for two consecutive windows"
    )
    claimed = (
        await conn.execute(
            text(
                "update wololo_evals.canary_gates set gate_status = 'regressed', "
                "regression_reason = :reason, rollback_key = :k, "
                "rollback_intent_at = now() "
                "where id = :id and rollback_key is null returning id"
            ),
            {"reason": reason, "k": rollback_key, "id": gate_id},
        )
    ).first()
    if claimed is None:
        # Lost the race to a concurrent poll; it owns the firing.
        return None
    await append_audit_event(
        conn, tenant_id, actor, "canary-regression-detected", gate.deploy_ref,
        decision="denied",
    )
    return _RollbackClaim(rollback_key, gate.deploy_ref)


async def confirm_rollback(
    conn, tenant_id: str, actor: str, gate_id: str, response_ok: bool
) -> str:
    """txn-2 phase: flip to 'rolled-back' ONLY on a confirmed 2xx, never
    optimistically. A non-2xx leaves the gate 'regressed' with intent intact
    for a retry job to re-send the same key."""
    if not response_ok:
        return "regressed"
    updated = (
        await conn.execute(
            text(
                "update wololo_evals.canary_gates set gate_status = 'rolled-back', "
                "rollback_confirmed_at = now(), closed_at = now() "
                "where id = :id and rollback_confirmed_at is null "
                "returning deploy_ref"
            ),
            {"id": gate_id},
        )
    ).first()
    if updated is not None:
        await append_audit_event(
            conn, tenant_id, actor, "canary-rollback-fired", updated.deploy_ref,
            decision="denied",
        )
    return "rolled-back"


async def run_canary_poll(
    engine, tenant_id: str, gate_id: str, deploy_client, *, actor: str = "canary-poller"
) -> str:
    """Full poll: detect+claim (txn 1) -> HTTP rollback (outside any txn, after
    the intent commit) -> confirm (txn 2). This is what the ``canary-poll`` job
    handler runs; the three-phase split is what makes rollback exactly-once and
    crash-safe."""
    async with tenant_txn(engine, tenant_id) as conn:
        claim = await detect_and_claim(conn, tenant_id, actor, gate_id)
    if claim is None:
        async with tenant_txn(engine, tenant_id) as conn:
            status = (
                await conn.execute(
                    text("select gate_status from wololo_evals.canary_gates where id = :id"),
                    {"id": gate_id},
                )
            ).scalar_one_or_none()
        return status or "missing"

    resp = await deploy_client.post(
        f"/canary/{claim.deploy_ref}/rollback",
        headers={"Idempotency-Key": claim.rollback_key},
    )
    ok = 200 <= resp.status_code < 300
    async with tenant_txn(engine, tenant_id) as conn:
        return await confirm_rollback(conn, tenant_id, actor, gate_id, ok)
