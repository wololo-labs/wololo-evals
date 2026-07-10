# Epic: wololo-evals — the quality gate for Wololo OS (wave 1: evals-core)

Task-file fallback for the beads epic (`bd` unavailable in this session, per the
GOAL's operating rules). Goal: `docs/goals/components/04-wololo-evals-GOAL.md` in
the kernel repo (fleet-lambda), first delivery wave. Branch: `f/evals-core`.

Wave-1 scope (from the run brief): Python service wrapping Inspect AI; skill-review
static evals; task-uplift evals with N>=5 paired trials + variance/CI + injection-
hardened LLM judge; suite_version/rubric_version pinning everywhere; per-tenant blob
storage for .eval logs; durable job queue with per-tenant concurrency + idempotency;
RLS tenancy per the four invariants; eval spec + schemas published open and mirrored
to fleet-lambda via BRANCH+PR. The canary-gate hook to wololo-deploy is a documented
stub this wave (06a/06c integration debt). Repo-eval, regression-replay, leaderboard
UI, Meridian rung-3 scenario and the harness-runner docker images are later waves.

## Tasks

- [ ] T1 epic seed committed (this file); repo created private; branch `f/evals-core`
- [ ] T2 contract families (SPEC S3): `eval-candidate` + `eval-result` (four kinds)
      schemas Apache-2.0 under `specs/`, builders, MUST-FAIL test vectors,
      registered in the kernel `version.py` registry — landed in fleet-lambda via
      branch `f/eval-contracts` + PR (never direct push); vendored here with pin
- [ ] T3 data model (SPEC S4): full `wololo_evals` DDL — tenants, candidates, runs,
      trials, scores, leaderboard, canary_gates, canary_telemetry, jobs,
      audit_events, audit_chain_heads; RLS enabled AND forced on every table with
      the inlined fail-closed `current_tenant_id()` resolver; forward-only
      `runs.status` trigger; n_trials floor-of-5 check
- [ ] T4 audit chain (SPEC S4): per-tenant hash chain serialized by the
      `audit_chain_heads` head-row lock; recompute + tamper + independence tests
- [ ] T5 payload encryption + blob store (SPEC S10): AES-256-GCM envelope
      encryption (`payload_ciphertext`/`payload_key_id`/`payload_sha256`, no
      plaintext column); tenant-prefixed blob store (fs + S3 backends), mismatch
      is 404 + denied audit row
- [ ] T6 durable job queue (SPEC S11): `FOR UPDATE SKIP LOCKED`, leases +
      heartbeats, exponential backoff, dead-lettering, idempotency keys,
      per-tenant `max_concurrent_runs` cap, round-robin fairness, daily quota 429
- [ ] T7 statistics engine (SPEC S7): paired trials, 95% Student-t CI over paired
      differences, wins rule (`ceil(0.6*n)` + `min_effect_pct`), `low_confidence`
      on CI spanning zero, golden math tests
- [ ] T8 judge hardening (SPEC S8): rubric-only system message, per-call 128-bit
      nonce fence, schema-scored structured output, fail-closed parse,
      `injection_suspected` tripwire -> `errored` + `judge-integrity-flagged`
      audit + human-review surfacing; k=5 median at temperature 0
- [ ] T9 version pinning (SPEC S9): `suite_version`/`rubric_version` content
      hashes computed at run creation, pinned on runs/scores/leaderboard rows,
      rubric archive content-addressed in the blob store
- [ ] T10 Inspect AI wiring (SPEC S6): `inspect_adapter.py` sole `inspect_ai`
      import; `harness_solver.py` subprocess bridge (sandbox().exec, fresh clone,
      profile-scoped skill install, transcript events, diff capture)
- [ ] T11 suites: `skill_review` (4 dimensions, weights .30/.30/.20/.20) and
      `task_uplift` (correctness .5 / turns .2 / approach .3 + canary weights)
      as Inspect `Task` definitions + rubric.yaml each
- [ ] T12 API + RBAC (SPEC S12/S13): POST /candidates, POST /candidates/:id/runs,
      GET /runs/:id, GET /runs/:id/trials, GET /leaderboard; entitlement token
      verified via the imported identity family against fixture JWKS; features
      claims as roles; token tenant only (body tenant never overrides); audit +
      OTEL + metering on every route
- [ ] T13 worker: run execution end-to-end — dispatch, trials, aggregate scores,
      composite frozen once at completion, verdict_stats, leaderboard upsert
- [ ] T14 stubs (GOAL STUBS): `deploy_stub.py` (idempotent rollback + call log,
      documented contract in specs/eval-result/README.md "For wololo-deploy"),
      `candidate_fixture_gen.py`; fixture JWKS dev keys
- [ ] T15 four invariant test families green on real Postgres: tenancy rows+blobs
      leak tests, audit chain, OTEL spans + metering + quota 429, entitlement
      missing/expired/wrong-role with row-count-unchanged proof
- [ ] T16 one minimal live eval if model access available (claude CLI OAuth is
      the only live path in this env), honestly labelled; docker-dependent
      harness-runner execution recorded as OPERATOR precondition
- [ ] T17 README, docker-compose.yml, docs/INTEGRATION-DEBT.md (contract pins +
      stub ledger), full suite green, branch pushed (no merge; merge phase owns
      that), fleet-lambda PROGRESS/INDEX status via BRANCH+PR

