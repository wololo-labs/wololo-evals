# wololo-evals

**The quality gate for Wololo OS: every skill, prompt, harness config and model swap must clear it mechanically before it is trusted.**

![Stack](https://img.shields.io/badge/stack-python-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)
![Licence](https://img.shields.io/badge/licence-proprietary-lightgrey)

Every change to an agent system ships a claim that it is an improvement.
wololo-evals is the checkpoint that claim has to clear: a Python service and CLI
that runs evaluations on top of [Inspect AI](https://inspect.aisi.org.uk/) and
adds paired-trial statistics, an injection-hardened judge, version-pinned
rubrics and a candidate-versus-baseline regression gate. It is the component the
rest of Wololo OS calls before a skill or a deployment is allowed to advance.

Design authority: `docs/goals/components/04-wololo-evals-SPEC.md` and
`docs/goals/DOCTRINE.md` in the wololo kernel repo (fleet-lambda).

---

## Visual proof

```
┌───────────────┐   ┌──────────────────┐   ┌────────────────────┐
│ CI / operator │──▶│ wololo-evals-cli │──▶│ engine adapters    │
│               │   │ matrix · gate    │   │ claude codex       │
└───────────────┘   └────────┬─────────┘   │ copilot replay     │
                             │             └─────────┬──────────┘
┌───────────────┐   ┌────────▼─────────┐             │ traces
│ wololo-deploy │──▶│ FastAPI service  │             ▼
│ canary hooks  │   │ /api/v1/...      │   ┌────────────────────┐
└───────────────┘   └────────┬─────────┘   │ Inspect AI suites  │
                             │             │ + LLM judge        │
                    ┌────────▼─────────┐   └─────────┬──────────┘
                    │ Postgres queue   │◀────────────┘
                    │ RLS + audit      │──▶ blob store (fs / s3)
                    └──────────────────┘
```

A real run against the corpus committed at
`examples/runs/dropdowns-synthetic/`, which records a skill upgrade that made
things worse. No model is called and no database is needed, so this is
reproducible from a fresh clone:

```console
$ wololo-evals-cli gate \
    --run examples/runs/dropdowns-synthetic/candidate-v2 \
    --baseline examples/runs/dropdowns-synthetic/baseline-v1 \
    --benchmark examples/runs/dropdowns-synthetic/benchmark \
    --golden-agreement 1.0
# Gate verdict: REGRESSION

- run: `examples/runs/dropdowns-synthetic/candidate-v2`
- baseline: `examples/runs/dropdowns-synthetic/baseline-v1`
- evaluation_type: `skill_behaviour`
- max_regression: `0.0`

**regression: CI upper bound -0.1069 < -0.0**

## Benchmark-score delta (95% CI)

- mean delta: `-0.3600`
- 95% CI: `[-0.6206, -0.1069]`
- significant regression: `True`
- significant improvement: `False`

## Four-bucket classification

| bucket | cases |
| --- | --- |
| working | 4 |
| fixed | 0 |
| regression | 4 |
| still_gap | 0 |

verdict written to examples/runs/dropdowns-synthetic/candidate-v2/gate_verdict.json
$ echo $?
1
```

Exit codes are the CI contract: `0` pass, `1` regression, `2` refused (a run
flagged `judge_disabled`, or a benchmark whose judge agreement is below the
calibration threshold).

---

## Getting started

### Prerequisites

- **Python 3.11 or newer** (`requires-python = ">=3.11"`).
- **PostgreSQL** for anything touching the service. The test suite starts a
  throwaway cluster from local binaries (`initdb` / `postgres` on `PATH` or under
  `/usr/lib/postgresql/*/bin`), so no Docker is needed for it. The compose stack
  uses the `postgres:16` image.
- **Docker** only for the container path: `docker-compose.yml` and the tests
  marked `compose`.

Mandatory environment variables, fail-closed with no default:

| Variable | Purpose |
|---|---|
| `WOLOLO_EVALS_DATABASE_URL` | Postgres connection string |
| `WOLOLO_EVALS_MASTER_KEY` | AES-256 master key, 64 hex chars, for envelope encryption |
| `WOLOLO_EVALS_JWKS` | Path to the entitlement-token issuer JWKS |

<details>
<summary>Optional environment variables and their defaults</summary>

| Variable | Default |
|---|---|
| `WOLOLO_EVALS_MASTER_KEY_ID` | `dev-master-1` |
| `WOLOLO_EVALS_KEYRING` | `./blobdata/keyring.json` |
| `WOLOLO_EVALS_ISSUER` | `wololo-deploy` |
| `WOLOLO_EVALS_BLOB_BACKEND` | `fs` (or `s3`) |
| `WOLOLO_EVALS_BLOB_ROOT` | `./blobdata/blobs` |
| `WOLOLO_EVALS_S3_BUCKET` | `wololo-evals` |
| `WOLOLO_EVALS_S3_ENDPOINT` | empty |
| `WOLOLO_EVALS_JUDGE_MODEL` | `claude-opus-4-1` |
| `WOLOLO_EVALS_JUDGE_BACKEND` | `http` |
| `WOLOLO_EVALS_JUDGE_BASE_URL` | `https://api.anthropic.com` |
| `WOLOLO_EVALS_JUDGE_API_KEY` | empty |
| `WOLOLO_EVALS_DEPLOY_BASE_URL` | `http://wololo-deploy-stub:8080` |

The DB-free eval core (the file-first CLI, a judge-only smoke) constructs only
the judge and blob sub-settings, so it needs none of the three mandatory vars.
</details>

### Quick install

```bash
pip install -e '.[dev]'      # add the s3 extra for the boto3 blob backend
pytest
```

### Smallest useful example

The `gate` command above is the fastest path to something working: it is pure
stdlib plus the eval core, no database, no engine, no network, no spend. A
non-zero exit and a `REGRESSION` verdict means it worked, because the committed
candidate really is a regression against the committed baseline.

To bring up the full service instead:

```bash
docker compose up -d --wait
```

That stands up Postgres, MinIO, the FastAPI service on port `8080`, the queue
worker, an OTEL collector on `4318`, and the wololo-deploy canary stub on `8090`.

---

## Ecosystem and architecture

### Features

- **Five eval suites**, each a task plus a weighted `rubric.yaml`:
  `skill-review`, `task-uplift`, `repo-eval`, `regression-replay`,
  `skill-behaviour`.
- **Paired-trial statistics**: baseline-versus-treatment arms with a DDL-enforced
  minimum of five trials per arm, a 95 percent Student-t confidence interval over
  paired differences, a wins rule and a `low_confidence` flag.
- **Candidate-versus-baseline gate**: paired bootstrap confidence interval on the
  benchmark-score delta, hard-gate trips, and a four-bucket classification of
  every case into working, fixed, regression or still-gap.
- **Injection-hardened judge**: rubric-only system message, a per-call 128-bit
  nonce fence around candidate content, schema-scored structured output,
  fail-closed parsing, and an `injection_suspected` tripwire that errors the run
  and raises it for human review.
- **Judge calibration as a precondition**: `judge calibrate` measures Cohen's
  kappa against a golden set, and the gate refuses to score judge-weighted
  dimensions when agreement is below threshold.
- **Version pinning**: `suite_version` and `rubric_version` content hashes are
  attached to every run, score and leaderboard row; comparisons never mix rubric
  versions, and rubric contents are archived content-addressed in the blob store.
- **Multi-engine harness**: adapters for `claude`, `codex` and `copilot`, plus an
  offline `replay` adapter that re-scores recorded archives at zero spend.
  Engine-native event streams are normalised into a single frozen trace schema.
- **Tenancy and audit**: row-level security on every table with a fail-closed
  tenant resolver, per-tenant append-only audit hash chains, AES-256-GCM envelope
  encryption for candidate payloads at rest, and tenant-prefixed blob storage.
- **Durable job queue**: Postgres `FOR UPDATE SKIP LOCKED` with leases,
  heartbeats, exponential backoff and dead-lettering, per-tenant concurrency caps,
  round-robin fairness, idempotency keys and a daily run quota returning 429.
- **Canary gate lifecycle**: open, telemetry and verdict routes plus a
  `canary-poll` worker with a two-consecutive-window regression debounce and an
  exactly-once, crash-safe idempotent rollback.
- **Observability**: OTEL traces and per-tenant metering on every route and run.

### Tech stack

| Layer | What is used |
|---|---|
| Language | Python 3.11+ |
| Eval engine | `inspect-ai` (imported from a single adapter module) |
| HTTP | FastAPI, Uvicorn, httpx |
| Data | SQLAlchemy 2 async, psycopg 3, PostgreSQL |
| Security | `cryptography`, PyJWT |
| Contracts | `jsonschema`, PyYAML |
| Telemetry | OpenTelemetry SDK and API |
| Blob storage | filesystem, or S3 and MinIO via the optional `boto3` extra |

### Interfaces

**HTTP** (all under `/api/v1`, all entitlement-authenticated and tenant-scoped):

| Method and path | Purpose |
|---|---|
| `POST /api/v1/candidates` | Submit an eval candidate |
| `POST /api/v1/candidates/{candidate_id}/runs` | Enqueue a run for a candidate |
| `GET /api/v1/runs/{run_id}` | Run status and verdict |
| `GET /api/v1/runs/{run_id}/trials` | Per-trial detail |
| `GET /api/v1/leaderboard` | Leaderboard entries |
| `POST /api/v1/leaderboard/rebuild` | Rebuild leaderboard rows |
| `POST /api/v1/canary-gates` | Open a canary gate |
| `POST /api/v1/canary-gates/{gate_id}/telemetry` | Push a telemetry window |
| `GET /api/v1/canary-gates/{gate_id}/verdict` | Poll the gate verdict |

**CLI** (`wololo-evals-cli`, installed by the package):

| Command | Purpose |
|---|---|
| `cases compile` | `cases.toml` to canonical `cases.jsonl`, with `--check` for drift |
| `matrix` | Expand and run the engine-by-model matrix; `--dry-run`, `--smoke`, `--max-cost-usd` |
| `bench` | Run one benchmark as a single combo |
| `replay` | Re-score an archived matrix run at zero spend |
| `gate` | Candidate-versus-baseline regression verdict for CI |
| `label` | Record a human verdict into `labels.jsonl` |
| `judge calibrate` | Golden-set judge agreement (Cohen's kappa) |

**Contracts consumed and published.** `specs/eval-candidate/` and
`specs/eval-result/` carry JSON Schemas plus valid and invalid test vectors for
the run input, `eval-run`, `eval-trial`, `eval-score` and `leaderboard-entry`.
`specs/identity/` carries the imported agent-identity and entitlement-token
schemas. These are vendored copies of families authored canonically in the
kernel repo; the pins are ledgered in
[`docs/INTEGRATION-DEBT.md`](docs/INTEGRATION-DEBT.md). The deploy-facing side of
the contract is documented in
[`specs/eval-result/README.md`](specs/eval-result/README.md).

Further reading: [`docs/ADVERSARIAL-VERIFY.md`](docs/ADVERSARIAL-VERIFY.md) for
the standing six-attack adversarial pass, and
[`docs/RUNBOOK-live-smoke.md`](docs/RUNBOOK-live-smoke.md) for the operator-gated
first live model trial.

---

## Status

| Capability | State | Notes |
|---|---|---|
| Contract schemas and test vectors | **Shipped** | `eval-candidate`, `eval-result`, imported `identity` |
| File-first CLI (`cases compile`, `matrix --dry-run`, `bench`, `replay`, `gate`, `label`, `judge calibrate`) | **Shipped** | Reproducible offline against the committed example corpus |
| Eval suites and rubrics | **Shipped** | Five suites, weights validated to sum to 1.0 at load |
| Paired-trial statistics and gate verdict | **Shipped** | Proven end to end by the committed replay archive |
| Injection-hardened judge and calibration refusal | **Shipped** | |
| FastAPI service, RLS tenancy, audit chain, envelope encryption | **Shipped** | Exercised against a real throwaway Postgres in the test suite |
| Durable queue and worker | **Shipped** | |
| Canary gate lifecycle and idempotent rollback | **Shipped** | Drives wololo-deploy through a documented stub, not the real integration |
| Engine adapters `claude` and `codex`, `replay` | **Shipped** | Adapters and trace normalisers are covered by fixture-driven tests |
| Compose stack and runner images | **Shipped, not continuously verified** | Needs a Docker daemon; the `compose` marker skips loudly without one |
| Live grading in `matrix` and `bench` | **In flight** | The CLI's default live grader emits no signals, so a caller must inject a real grader or cases score zero |
| Engine adapter `copilot` live path | **In flight** | Only replayed from recorded fixtures so far; live validation needs a working `COPILOT_GITHUB_TOKEN` |
| First live model trial | **Planned** | Runbook written and spend-gated; nothing in the repo spends money by default |
| Real wololo-deploy integration | **Planned** | Flagger confirm-rollout replaces `stubs/deploy_stub.py` |
| Real entitlement issuer | **Planned** | Fixture JWKS today; the validation code path is unchanged when the real issuer lands |
| Broker-issued per-run model credentials | **Planned** | Static scoped key today |
| Real candidate source | **Planned** | `stubs/candidate_fixture_gen.py` stands in until autoresearch submits real candidates |
| Continuous integration for tests | **Planned** | No test workflow is defined in this repository yet |

Every stub above is ledgered with its owner and its swap in
[`docs/INTEGRATION-DEBT.md`](docs/INTEGRATION-DEBT.md).

---

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). Every change to `main` goes through a
pull request approved by [@stevengonsalvez](https://github.com/stevengonsalvez).

## Licence

The service, meaning `src/`, `tests/` and everything not listed below, is
proprietary and closed. See [`LICENSE`](LICENSE): copyright 2026 Steven
Gonsalvez, all rights reserved, no licence granted except under a separately
executed commercial agreement.

These subtrees carry their own Apache-2.0 `LICENSE` file and are published open:
`specs/eval-candidate/`, `specs/eval-result/`, `src/wololo_evals/cli/`,
`src/wololo_evals/trace/`, `src/wololo_evals/engines/`, `fixtures/browser/` and
`examples/runs/dropdowns-synthetic/`.
