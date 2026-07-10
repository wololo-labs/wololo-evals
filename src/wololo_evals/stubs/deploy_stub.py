"""wololo-deploy canary-hook stub (GOAL STUBS #1, owner: 06c).

A minimal stand-in for the future wololo-deploy rollback endpoint so the
canary lifecycle is exercisable end to end today. The one behaviour that MUST
be real is idempotency: a replayed ``Idempotency-Key`` returns the original
response and does NOT increment the call log, which is exactly what proves
"exactly-once rollback across a worker crash" in the adversarial-verify pass.

The real contract this stub imitates is documented in
``specs/eval-result/README.md`` ("For wololo-deploy"): gate open, raw
telemetry POST, ``GET .../verdict`` (the Flagger confirm-rollout read,
DOCTRINE 15.5), idempotent rollback keyed by ``Idempotency-Key``. When 06c
lands, this file is deleted and the Meridian scenario repoints at the real
Flagger integration (integration debt, ledgered).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse


@dataclass
class RollbackLog:
    """In-memory record of rollback calls the test/scenario asserts against."""

    calls: list[dict] = field(default_factory=list)
    # idempotency-key -> the response body we returned the first time
    _seen: dict[str, dict] = field(default_factory=dict)

    def record(self, deploy_ref: str, idempotency_key: str | None) -> tuple[dict, bool]:
        """Returns (response_body, was_replay). A replayed key does not append
        to ``calls`` and returns the original body."""
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key], True
        body = {"rolled_back": True, "deploy_ref": deploy_ref}
        self.calls.append({"deploy_ref": deploy_ref, "idempotency_key": idempotency_key})
        if idempotency_key:
            self._seen[idempotency_key] = body
        return body, False


def create_deploy_stub(log: RollbackLog | None = None) -> FastAPI:
    app = FastAPI(title="wololo-deploy-stub", version="0.0.1")
    app.state.log = log or RollbackLog()

    @app.post("/canary/{deploy_ref}/rollback")
    async def rollback(
        deploy_ref: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        body, replay = app.state.log.record(deploy_ref, idempotency_key)
        # Idempotent: same key => original body, 200, call log unchanged.
        return JSONResponse(status_code=200, content=body)

    @app.get("/canary/{deploy_ref}/calls")
    async def calls(deploy_ref: str):
        return {
            "deploy_ref": deploy_ref,
            "calls": [c for c in app.state.log.calls if c["deploy_ref"] == deploy_ref],
        }

    return app


async def send_telemetry_window(
    client, gate_id: str, window_id: str, metrics: dict, sample_count: int, observed_at: str
) -> "httpx.Response":  # noqa: F821  (httpx typed at call site)
    """Telemetry-sender fixture: POST a RAW-metric window to wololo-evals.

    Raw metrics only — a ``composite`` field is a 422 on the evals side (the
    gate computes the live composite itself under the pinned rubric). This
    helper is what the scenario/telemetry driver calls to feed regressing
    windows through the canary lifecycle.
    """
    return await client.post(
        f"/api/v1/canary-gates/{gate_id}/telemetry",
        json={
            "window_id": window_id,
            "metrics": metrics,
            "sample_count": sample_count,
            "observed_at": observed_at,
        },
    )
