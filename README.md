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

- **Six eval suites**, each a task plus a weighted `rubric.yaml`:
  `skill-review`, `task-uplift`, `repo-eval`, `regression-replay`,
  `skill-behaviour`, `qe-review`. The registry is open: an out-of-tree suite
  registers through the `wololo_evals.suites` entry-point group, the
  `WOLOLO_EVALS_SUITE_DIRS` environment override, or `register_suite()`.
- **A grader that dispatches by assertion kind**: `trace:` assertions to the
  trajectory engine, `code:` assertions to the acceptance runner, plain text to
  the hardened judge. A check that could not be evaluated emits
  `evaluator_error`, never a zero.
- **Mechanical QE scoring**: a mutation kill rate over `mutmut`, a seeded-defect
  catch rate, and a four-dimension rule-based `qe-review` rubric, all computed
  at gate time and all at zero spend. See
  [the QE benchmark](#the-qe-benchmark-scoring-a-testing-skill).
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
| `matrix` | Expand and run the engine-by-model matrix; `--dry-run`, `--smoke`, `--max-cost-usd`, `--judge on` |
| `bench` | Run one benchmark as a single combo; `--judge on` |
| `replay` | Re-score an archived matrix run at zero spend |
| `gate` | Candidate-versus-baseline regression verdict for CI; `--signal-guard <id>` fails on a named signal regressing |
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

## The QE benchmark: scoring a testing skill

A skill that writes tests is easy to praise and hard to measure: the suite it
produces runs green either way. `benchmarks/qe-skill` measures it instead, over
`examples/fixtures/quoting-service` - a parcel quoting service in ~190 lines of
standard library Python, shipping with the seven-test suite a reviewer would
wave through.

```
┌─────────────────┐   ┌──────────────────┐   ┌───────────────────┐
│ recorded run    │──▶│ ReplayAdapter    │──▶│ production grader │
│ raw stream (fx) │   │ re-emit stream   │   │                   │
│ generated tests │   │ restore workspace│   │ trace: ──▶ trace  │
└─────────────────┘   └──────────────────┘   │ code:  ──▶ accept │
                                             │ text   ──▶ judge  │
                              ┌──────────────┤                   │
                              │              │ + mutation        │
                              ▼              │ + seeded defects  │
                    ┌───────────────────┐    │ + qe-review       │
                    │ gate              │◀───┤                   │
                    │ 0 pass            │    └───────────────────┘
                    │ 1 regression      │
                    │ 2 refused         │
                    └───────────────────┘
```

### What is scored

Twelve cases: eleven asking for tests over one slice of the service, and one
distractor with no testing work in it, where the correct behaviour is to answer
and not activate. Every case is scored on the four `skill_behaviour` gate slots,
and the `task_outcome` slot is where the QE number lives:

| Signal | What it asks | Baseline | Candidate |
|---|---|---:|---:|
| `mutation_kill_rate` | of every mutant of `quoting/`, how many does the suite notice | 0.2373 | 0.3012 |
| `seeded_defects_caught` | of the four defects in `seeded_defects.json`, how many does it go red on | 0.2500 | 0.3636 |
| `qe_rubric` | the weighted `qe-review` composite | 0.8782 | 0.9033 |
| `qe_assertion_strength` | share of tests making a discriminating expectation | 0.8571 | 0.8959 |
| `qe_changed_line_coverage` | share of the declared changed lines executed | 0.7368 | 0.7847 |
| `qe_flake_resistance` | share of tests whose outcome holds across hash seeds | 1.0000 | 1.0000 |
| `qe_no_tautological_assertions` | share of assertions that can actually fail | 1.0000 | 0.9943 |

Baseline is the no-agent arm: the repository's shipped suite, run as it stands.
Every one of those numbers is **recomputed at gate time** from the tests the
recorded run left in its workspace - the corpus records what each run *did*, not
what it *scored*, so changing a scorer or a rubric weight moves the number.

**The recorded streams are hand-authored fixtures.** `examples/runs/qe-quoting-synthetic/`
holds trajectories written to the Claude Code `stream-json` shape, not captured
from a model run: their `provenance.json` says `model_id:
synthetic:hand-authored` and carries no token counts, timestamps or session ids,
because no model produced them. They settle the *trajectory* questions (did the
skill activate, did it read before writing, did it leave the code under test
alone) and should be read as authored answers to those. Every *quality* number
in the table above is absent from the archive and is computed by running code:
`mutmut` really mutates `quoting/`, each seeded defect is really planted, and
the suites really run. The test modules the candidate arm "wrote" are real code
that really executes, and are what all of it is computed from.

`qe_no_tautological_assertions` slipping by 0.0057 is real and is reported
rather than smoothed away: one candidate case asserts a property of its own rate
table, which pins nothing about the service. That is what the per-signal table
is for.

### Running it

```console
$ pip install -e '.[dev]'            # or '.[mutation]' for just the QE scorers
$ wololo-evals-cli gate     --run      examples/runs/qe-quoting-synthetic/recorded/candidate     --baseline examples/runs/qe-quoting-synthetic/recorded/baseline     --signal-guard mutation_kill_rate     --signal-guard seeded_defects_caught     --signal-guard qe_flake_resistance
# Gate verdict: PASS
...
| signal                    | slot         | baseline | candidate |   delta |          |
| `mutation_kill_rate`      | task_outcome |   0.2373 |    0.3012 | +0.0639 | GUARDED  |
| `seeded_defects_caught`   | task_outcome |   0.2500 |    0.3636 | +0.1136 | GUARDED  |
| `qe_flake_resistance`     | -            |   1.0000 |    1.0000 | +0.0000 | GUARDED  |
$ echo $?
0
```

To recompute the pair from the archives rather than reading the committed one
(about 100 seconds, nearly all of it mutation analysis):

```console
$ wololo-evals-cli matrix --config examples/runs/qe-quoting-synthetic/matrix-baseline.toml
$ wololo-evals-cli matrix --config examples/runs/qe-quoting-synthetic/matrix-candidate.toml
```

`--signal-guard <id>` fails the gate when that signal's mean regressed. A rising
composite can hide a measure slipping underneath it; a guard pins the ones that
must not move the wrong way. A guard naming a signal absent from either run
refuses (exit 2) rather than reporting a guard that measured nothing.

### Which paths are live and which are fixture

Nothing in the table above calls a model. Being explicit about that matters more
than the numbers.

The judge is the only part of `matrix` and `bench` that spends anything, and it
is **off unless asked**: `--judge on`, or `WOLOLO_EVALS_JUDGE=on`. It is
deliberately not keyed on `ANTHROPIC_API_KEY` being present, because that would
flip every caller who exports a key for some other reason from free to billing
on their next run. With the judge on and no key set, the run refuses rather than
scoring every judge assertion `skipped`, which would look like a cheap pass on
the one run someone asked to be judged.

| Path | State | Why |
|---|---|---|
| `trace:` assertions | **Live** | Pure functions over the normalised trace |
| `code:` assertions | **Live** | AST-allow-listed expressions over the run's outputs and the QE scorers' live results |
| Recorded engine streams | **Fixture** | Hand-authored to the Claude Code `stream-json` shape and labelled as such in every `provenance.json`; they drive the replay engine, and the `trace:` assertions read them |
| `mutation_kill_rate` | **Live** | `mutmut` really mutates `quoting/` and really runs the suite, on every gate run |
| `seeded_defects_caught` | **Live** | Each declared defect is planted into a staged copy and the suite is re-run |
| `qe-review` rubric | **Live** | Two AST dimensions, two that run the suite (repeated execution, `coverage`) |
| LLM judge | **Fixture** | Off by default, so judge assertions score `skipped` and are excluded from the slot denominators. Opt in with `--judge on` or `WOLOLO_EVALS_JUDGE=on`, which then requires `ANTHROPIC_API_KEY` |
| Judge calibration (kappa) | **Not run** | No golden-set agreement record is committed for `qe-skill`. The judge does not participate, so the gate does not refuse; enabling the judge requires `judge calibrate` first |
| Engine adapters (`claude`, `codex`) | **Fixture** | Both arms replay hand-authored streams; no engine subprocess runs |
| Copilot adapter | **Fixture** | Replay only. A live smoke needs a Copilot token in the environment; without one the recorded path is used and labelled here |

The seeded defects live in `seeded_defects.json` rather than in the fixture's
code because mutation analysis needs a green baseline: a suite that cannot run
cannot be scored. Each defect is an exact-string edit applied to a staged copy,
and an edit that no longer matches fails loudly instead of reading as a defect
nobody caught.

### CI

[`.github/workflows/eval-gate.yml`](.github/workflows/eval-gate.yml) runs the
test suite, checks the committed casebooks compile to what is committed, then
runs two gates. The `skill-behaviour` gate over `examples/runs/dropdowns-synthetic`
**must exit 1** - that corpus records a skill upgrade that made things worse, so
a green verdict there would mean the gate cannot fail. The QE gate must exit 0,
and a final step reads the verdict JSON and fails on the numbers as well as the
exit code.

---

## Status

| Capability | State | Notes |
|---|---|---|
| Contract schemas and test vectors | **Shipped** | `eval-candidate`, `eval-result`, imported `identity` |
| File-first CLI (`cases compile`, `matrix --dry-run`, `bench`, `replay`, `gate`, `label`, `judge calibrate`) | **Shipped** | Reproducible offline against the committed example corpus |
| Eval suites and rubrics | **Shipped** | Six suites, weights validated to sum to 1.0 at load; registry open to out-of-tree suites |
| Production grader (`trace:`, `code:`, judge dispatch) | **Shipped** | Injected by default into `matrix` and `bench`; proven above zero on a live-shaped run |
| QE benchmark and mechanical scorers | **Shipped** | 12 cases over `examples/fixtures/quoting-service`; mutation kill rate, seeded-defect catch rate and the `qe-review` rubric recomputed at gate time. The recorded trajectories are hand-authored fixtures; a live capture is follow-up work |
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
`src/wololo_evals/trace/`, `src/wololo_evals/engines/`,
`benchmarks/dropdowns-skill/`, `fixtures/browser/`, `fixtures/synthetic-skill/`
and `examples/runs/dropdowns-synthetic/`.
