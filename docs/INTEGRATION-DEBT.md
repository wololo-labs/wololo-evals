# Integration debt & contract pins (wololo-evals)

Explicit, ledgered seams to re-wire once neighbour goals land (DOCTRINE 15.3:
every stub's real producer has a named OWNER goal). Nothing here is a silent
gap; each row names what is stubbed, why, and the swap.

## Contract pins (open, Apache-2.0)

The `eval-candidate` and `eval-result` families are authored canonically in the
kernel repo (fleet-lambda) `core/contracts/` + `specs/` and published open. They
land there via **branch `f/eval-contracts` + PR #57** (never a direct push). The
copies under `specs/eval-candidate/`, `specs/eval-result/`, and
`src/wololo_evals/contracts/` here are **vendored** at that pin. The imported
`identity` family (`specs/identity/`, `src/wololo_evals/contracts/identity.py`)
is vendored the same way; wololo-evals never hand-rolls a token interface
(DOCTRINE 15.1). Re-sync when PR #57 merges and on every family MINOR bump.

## Stub ledger

| Stub | File | Owner goal | Real contract it imitates | Swap |
|---|---|---|---|---|
| wololo-deploy canary hook | `src/wololo_evals/stubs/deploy_stub.py` | 06c | Idempotent rollback keyed by `Idempotency-Key`; gate-open; raw telemetry POST; `GET .../verdict` (Flagger confirm-rollout, DOCTRINE 15.5) — documented in `specs/eval-result/README.md` "For wololo-deploy" | Delete stub; point Meridian scenario at real Flagger integration polling `GET /api/v1/canary-gates/:id/verdict` |
| autoresearch candidate source | `src/wololo_evals/stubs/candidate_fixture_gen.py` | 09 | `eval-candidate` submissions (skill/prompt variants) | Retire generator; autoresearch submits real candidates; wire cerebro tech-signal digest as a second source |
| Entitlement issuer (JWKS) | `tests/fixtures/dev-jwks.json` + `specs/identity/test-vectors/` | 06a | Entitlement token signed by `wololo-licence`, keys by `kid` from the deploy-published JWKS | Swap fixture JWKS path for the 06a JWKS URL; **same validation code path** |
| Per-run model API key | compose env `WOLOLO_EVALS_JUDGE_API_KEY` | 03 (wololo-broker) | Short-TTL per-run credential | Broker issues per-run keys; egress allowlist becomes broker-policy-driven |

## Re-wire once neighbours land (SPEC S16)

- **02 reflect-teams:** reconcile the self-contained RLS resolver + persistence
  skeleton (`migrations/001_init.sql`, `db.py`) with reflect-teams'
  `current_tenant_id()` lineage into one shared closed-services library
  (deliberately duplicated for self-containment today).
- **13 fleet-scheduler:** migrate or front the Postgres `jobs` queue with the
  org-wide durable scheduler; per-tenant fairness moves to its admission policy.
  `# ponytail: Postgres SKIP LOCKED queue, ~10k jobs/min ceiling; fleet-scheduler is the upgrade path.`
- **05 workbench:** browsable leaderboard UI over `GET /api/v1/leaderboard`;
  signed verdict attestations join workbench's provenance family.
- **11 runtime-sandbox:** align harness-runner container hardening with the
  fleet's default execution-cell policy.

## Environment preconditions (this build ran degraded — recorded honestly)

Rung 1 (unit/contract/invariant) runs **fully green** against a real local
Postgres binary with no Docker daemon. The following need infra absent from the
reference build environment and are OPERATOR preconditions, not skipped work:

- **Rung 2 (component compose):** `docker compose up -d --wait` then the
  integration suite. No Docker daemon here. Compose, Dockerfile, and OTEL
  collector config are committed and runnable where a daemon exists.
- **Live harness-under-eval (task-uplift + repo-eval, SPEC S6):** needs the
  per-harness docker sandbox images + a model API key for a *containerized*
  trial. What is proven vs what is an operator precondition, stated precisely:
  the orchestration is proven via an injected exec seam, and the real
  sandbox execution is an operator precondition. `worker.execute_run` drives
  task-uplift through `inspect_adapter.run_task_uplift` (clone -> profile-scoped
  skill install -> harness loop -> transcript turns -> diff capture ->
  acceptance test -> judge -> paired uplift stats -> frozen composite +
  leaderboard row) and repo-eval through `run_repo_eval`, tested on both Claude
  Code and Hermes in `tests/test_harness_uplift.py` and `tests/test_repo_eval.py`.
  That seam proves the wiring and arm sequencing; it does NOT execute the
  production path (`exec_factory=None -> _sandbox_exec -> sandbox().exec`
  running the real `claude`/`hermes` CLI in a network-disabled per-trial
  container), which no CI here runs because there is no docker daemon. A real
  containerized trial per harness is gated behind the compose integration suite
  (`tests/test_compose_integration.py`, marked `compose`) and runs once a daemon
  + key exist. Until then this reads as "orchestration proven via exec seam;
  real sandbox execution is an operator precondition", never "proven end to end".
- **Live judge model call:** no `ANTHROPIC_API_KEY` in the build environment.
  The claude CLI bridge (`judge.cli_call_model`) and the HTTP backend
  (`judge.http_call_model`) are committed; the skill-review pipeline is proven
  end to end with a deterministic injected judge (`tests/test_worker.py`). A
  live judge run swaps the injected `call_model` for either backend, no other
  change.
- **Rung 3 (Meridian gate):** needs the standing Meridian fixture (Atlas seeded
  repos) stood up. Later wave; scenario authoring tracked in the epic.
