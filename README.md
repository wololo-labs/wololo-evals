# wololo-evals

The quality gate for Wololo OS. Every skill, prompt change, harness config and model
swap ships a claim about being an improvement; wololo-evals is the checkpoint that
claim must clear, mechanically, before it is trusted: a Python service that runs
evaluations on top of [Inspect AI](https://inspect.aisi.org.uk/) and adds this OS's
own IP on top.

Design authority: `docs/goals/components/04-wololo-evals-SPEC.md` in the wololo
kernel repo (fleet-lambda), plus `docs/goals/DOCTRINE.md` (Section 15 amendments
binding).

## Licence

- The service (`src/`, `tests/`, everything not listed below) is **proprietary,
  closed, entitlement-keyed** (DOCTRINE Section 1, Closed row). See `LICENSE`.
- `specs/eval-candidate/` and `specs/eval-result/` are **Apache-2.0**, published
  open for credibility (DOCTRINE Section 11). Each carries its own `LICENSE` file.
  Canonical home of these contract families is the kernel repo's `core/contracts`
  + `specs/` tree; the copies here are vendored (see `docs/INTEGRATION-DEBT.md`
  for the pin).

## What is in this delivery wave (f/evals-core)

- Eval execution service wrapping Inspect AI (`src/wololo_evals/inspect_adapter.py`
  is the ONLY module importing `inspect_ai`).
- Skill-review static evals: `validation_gate` / `implementation` / `activation` /
  `review` dimensions, weighted composite, tenant pass bar.
- Task-uplift evals: paired baseline-vs-treatment trials, N>=5 per arm (DDL-enforced
  floor), 95% Student-t confidence interval over paired differences, wins rule,
  `low_confidence` flag.
- Repo-eval evals: `generate.py` walks a repo's real seeded history, picks an
  isolable `fix:`/`feat:` commit and scores a single-arm harness run on
  `correctness` (acceptance test in-sandbox), `churn_penalty` (rule-based off diff
  line counts vs the historical fix) and `abstraction_adherence` (hardened judge).
- Regression-replay evals: replays the corrections corpus, scoring whether a
  candidate would avoid needing the same human correction again; each correction's
  trusted `signal_class`/`matched_signals` reference is woven into the system
  rubric while the candidate stays nonce-fenced.
- Injection-hardened LLM judge: rubric-only system message, per-call 128-bit nonce
  fence around candidate content, schema-scored structured output, fail-closed
  parsing, `injection_suspected` tripwire -> run `errored` + audit + human review.
- `suite_version` + `rubric_version` (content hashes) pinned on every run, score
  and leaderboard row; comparisons never mix rubric versions; rubric contents
  archived content-addressed in the blob store.
- Per-tenant blob storage for raw `.eval` logs (`evals/{tenant_id}/{run_id}/...`)
  with resolved-tenant prefix checks (404 + denied audit on mismatch).
- Durable Postgres job queue (`FOR UPDATE SKIP LOCKED`, leases, heartbeats,
  exponential backoff, dead-lettering) with per-tenant concurrency caps,
  round-robin fairness and idempotency keys; daily run quota returns 429.
- Row-level security on every table with the inlined fail-closed
  `wololo_evals.current_tenant_id()` resolver; per-tenant append-only audit hash
  chains; AES-256-GCM envelope encryption for candidate payloads at rest;
  OTEL traces + per-tenant metering on every route and run.
- Entitlement verification against the imported `identity` contract family
  (fixture JWKS until wololo-deploy 06a, the sole real issuer, ships).

The canary gate lifecycle is **live** — open/telemetry/verdict routes plus the
`canary-poll` worker with a two-consecutive-window regression debounce and an
exactly-once, crash-safe idempotent rollback (`src/wololo_evals/canary.py`,
`tests/test_canary.py`). It drives `wololo-deploy` through a **documented stub**
(`src/wololo_evals/stubs/deploy_stub.py` + the "For wololo-deploy" section of
`specs/eval-result/README.md`); swapping the stub for the real Flagger
confirm-rollout integration is 06c integration debt, tracked in
`docs/INTEGRATION-DEBT.md`.

## Layout

```
specs/eval-candidate/     Apache-2.0: schema + test vectors for the run input
specs/eval-result/        Apache-2.0: eval-run / eval-trial / eval-score /
                          leaderboard-entry schemas + deploy-facing contract doc
src/wololo_evals/
  contracts/              vendored kernel contract builders (pin in INTEGRATION-DEBT)
  migrations/001_init.sql full DDL: tables, RLS, status trigger, audit chain
  db.py                   async engine + tenant_txn (transaction-local GUC scope)
  audit.py                per-tenant hash-chained audit appender
  crypto.py               AES-256-GCM envelope encryption for candidate payloads
  blobstore.py            tenant-prefixed blob store (filesystem + S3 backends)
  versioning.py           suite/rubric content hashes + rubric archive
  stats.py                paired-trial statistics + pass rule
  judge.py                injection-hardened judge call shape
  inspect_adapter.py      the sole inspect_ai import boundary
  harness_solver.py       harness-under-eval subprocess solver (SPEC S6)
  suites/                 skill_review, task_uplift, repo_eval (+generate.py),
                          regression_replay (task.py + rubric.yaml each)
  jobs/                   durable queue + worker
  api/                    FastAPI surface (entitlement auth, RBAC, quota, audit)
  stubs/                  deploy_stub, candidate_fixture_gen (named neighbours)
tests/                    real-Postgres invariant + unit + e2e suites
docs/INTEGRATION-DEBT.md  vendored pins + stub ledger
```

## Running the tests

Tests run against a real throwaway PostgreSQL cluster started from local binaries
(`initdb`/`postgres` on PATH or under `/usr/lib/postgresql/*/bin`); no Docker
needed. `pip install -e '.[dev]'` then `pytest`.

`docker-compose.yml` stands up Postgres + MinIO + API + worker + OTEL collector +
the deploy stub for the container path (validated where a Docker daemon exists).
