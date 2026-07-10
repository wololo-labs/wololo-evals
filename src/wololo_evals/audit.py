"""Per-tenant append-only audit hash chains (invariant 2, SPEC S4).

Every state-changing handler calls :func:`append_audit_event` inside its own
transaction. Serialization per tenant is by the ``audit_chain_heads`` head-row
lock (``select ... for update``), exactly the shape reflect-teams (02) uses;
two tenants' chains are fully independent.

``this_hash = sha256(prev_hash || canonical-json(tenant_id, seq, actor,
action, resource, decision, ts))`` where ts is the UTC isoformat string also
stored on the row. :func:`recompute_chain` re-derives a tenant's whole chain
for the tamper-evidence tests and any external verifier.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import text

GENESIS = "genesis"

AUDIT_ACTIONS = (
    "candidate-submitted",
    "run-started",
    "run-completed",
    "canary-opened",
    "canary-regression-detected",
    "canary-rollback-fired",
    "quota-rejected",
    "judge-integrity-flagged",
    "leaderboard-rebuilt",
    "blob-access-denied",
    "auth-denied",
)


def _event_hash(
    prev_hash: str,
    tenant_id: str,
    seq: int,
    actor: str,
    action: str,
    resource: str,
    decision: str,
    ts: str,
) -> str:
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "seq": seq,
            "actor": actor,
            "action": action,
            "resource": resource,
            "decision": decision,
            "ts": ts,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def append_audit_event(
    conn,
    tenant_id: str,
    actor: str,
    action: str,
    resource: str,
    decision: str = "allowed",
) -> dict:
    """Append one audit event to *tenant_id*'s chain, inside the caller's txn."""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"action {action!r} not in {AUDIT_ACTIONS}")
    if decision not in ("allowed", "denied"):
        raise ValueError(f"decision {decision!r} must be allowed|denied")

    head = (
        await conn.execute(
            text(
                "select last_seq, last_hash from wololo_evals.audit_chain_heads "
                "where tenant_id = :t for update"
            ),
            {"t": tenant_id},
        )
    ).first()
    if head is None:
        seq, prev_hash = 0, GENESIS
        await conn.execute(
            text(
                "insert into wololo_evals.audit_chain_heads (tenant_id, last_seq, last_hash) "
                "values (:t, -1, :h)"
            ),
            {"t": tenant_id, "h": GENESIS},
        )
    else:
        seq, prev_hash = head.last_seq + 1, head.last_hash

    ts = datetime.now(timezone.utc).isoformat()
    this_hash = _event_hash(prev_hash, tenant_id, seq, actor, action, resource, decision, ts)

    await conn.execute(
        text(
            "insert into wololo_evals.audit_events "
            "(tenant_id, seq, actor, action, resource, decision, ts, prev_hash, this_hash) "
            "values (:t, :seq, :actor, :action, :resource, :decision, "
            "cast(:ts as timestamptz), :prev, :this)"
        ),
        {
            "t": tenant_id,
            "seq": seq,
            "actor": actor,
            "action": action,
            "resource": resource,
            "decision": decision,
            "ts": ts,
            "prev": prev_hash,
            "this": this_hash,
        },
    )
    await conn.execute(
        text(
            "update wololo_evals.audit_chain_heads set last_seq = :seq, last_hash = :h "
            "where tenant_id = :t"
        ),
        {"seq": seq, "h": this_hash, "t": tenant_id},
    )
    return {"tenant_id": tenant_id, "seq": seq, "this_hash": this_hash, "ts": ts}


async def recompute_chain(conn, tenant_id: str) -> tuple[bool, str]:
    """Recompute *tenant_id*'s chain from its rows.

    Returns ``(intact, detail)``: ``intact`` is False on the first hash
    mismatch or sequence gap, with the offending seq named in ``detail``.
    """
    rows = (
        await conn.execute(
            text(
                "select seq, actor, action, resource, decision, ts, prev_hash, this_hash "
                "from wololo_evals.audit_events where tenant_id = :t order by seq"
            ),
            {"t": tenant_id},
        )
    ).all()
    prev = GENESIS
    for i, row in enumerate(rows):
        if row.seq != i:
            return False, f"sequence gap at seq {row.seq} (expected {i})"
        if row.prev_hash != prev:
            return False, f"prev_hash mismatch at seq {row.seq}"
        expected = _event_hash(
            prev,
            tenant_id,
            row.seq,
            row.actor,
            row.action,
            row.resource,
            row.decision,
            row.ts.isoformat(),
        )
        if row.this_hash != expected:
            return False, f"hash mismatch at seq {row.seq}"
        prev = row.this_hash
    return True, f"intact over {len(rows)} events"
