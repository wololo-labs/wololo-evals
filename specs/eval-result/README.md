# specs/eval-result

The verdict contract family of the Wololo OS quality gate (04-wololo-evals
SPEC Section 3.2). Four kinds, one per granularity, all `schemaVersion: 1`:

| Kind | What it is |
|---|---|
| `eval-run` | One evaluation of a candidate: an aggregate over `n_trials` trials per arm, with frozen `composite_score` + `verdict_stats` |
| `eval-trial` | One trial (arm + index). First-class so n=1 verdicts are structurally impossible to hide (DOCTRINE 15.6) |
| `eval-score` | One scored dimension; `trial_index` null = aggregate, int = per-trial raw (including the k=5 raw judge samples) |
| `leaderboard-entry` | One (model, harness, skill) ranking row; a rebuildable read-model over runs |

This directory publishes **Apache-2.0** (DOCTRINE Section 11). Builders:
`core/contracts/eval_result.py`; registry: `core/contracts/version.py`; drift
tests: `tests/test_contract_schemas.py` and `tests/test_eval_contract.py`.

## Semantics that are part of the contract

- **One-way run status.** `queued -> running -> {passed, failed, errored}`,
  enforced at the persistence layer (a status regression would corrupt the
  canary gate's read of "did this already resolve").
- **Composite selection rule.** The composite is computed exactly once, at run
  completion, and frozen onto the run; consumers (leaderboard, canary) read
  only that; a re-score is a NEW run with its own pinned versions.
- **Version pinning (SPEC Section 9).** `suite_version` (content hash of the
  suite directory) and `rubric_version` (content hash of `rubric.yaml`) are
  required 64-hex fields on every run, score and leaderboard row. Rankings and
  canary comparisons never mix rubric versions.
- **Statistics (DOCTRINE 15.6).** `verdict_stats` carries
  `{mean, stddev, ci_low, ci_high, wins, losses, n, low_confidence}` — a 95%
  Student-t interval over paired per-trial differences. A CI spanning zero
  sets `low_confidence: true`, which blocks canary-gate opening.

## For wololo-deploy (the canary seam, owner: goals 06a/06c)

wololo-evals ships the seam now; wololo-deploy integrates without a
wololo-evals change. The contract wololo-deploy must implement/consume —
stubbed today by `wololo-evals`' `deploy_stub.py`, replaced by the real
Flagger/Argo confirm-rollout integration when 06c lands:

1. **Gate open** — `POST /api/v1/canary-gates` with
   `{candidate_id, run_id, deploy_ref}` when a candidate goes live for a
   canary cohort. Rejected 409 unless the referenced run is `passed` and not
   `low_confidence`. At open, the gate pins the run's `rubric_version` and
   leaves `baseline_composite` **NULL**; the baseline is frozen from the first
   telemetry window (see below), not from the run's eval composite.

   > **Resolved inconsistency.** An earlier draft said the gate "copies the
   > run's frozen composite as `baseline_composite`". That is a category
   > error: the run's `composite_score` is an eval-dimension composite
   > (correctness/turns/approach, ~0.89) while the live composite is a
   > `canary_weights` sum over operational metrics
   > (task_success_rate/error_rate/tokens, ~0.54). Comparing across the two
   > spaces trips a false regression on healthy traffic. The baseline must be
   > computed by the SAME rule as the live windows, so it is the first
   > telemetry window's live composite.
2. **Raw telemetry, never a composite** —
   `POST /api/v1/canary-gates/:id/telemetry` with
   `{window_id, metrics: {task_success_rate, error_rate, tokens_per_task, ...},
   sample_count, observed_at}`. wololo-evals computes the live composite
   ITSELF from the pinned rubric's canary weights; a body containing a
   `composite` field is a 422. Windows dedupe on `(gate_id, window_id)`. The
   FIRST window a gate receives is the stable / pre-shift cohort: its live
   composite is frozen once as `baseline_composite` (first-writer wins), so
   baseline and every later window share one metric space.
3. **Verdict read (Flagger confirm-rollout shape, DOCTRINE 15.5)** —
   `GET /api/v1/canary-gates/:id/verdict` returns
   `{gate_status, live_composite, baseline_composite, windows_observed}`.
4. **Idempotent rollback** — on regression (live composite more than the
   tenant's `regression_threshold_pct` below baseline for TWO consecutive
   windows), wololo-evals calls `POST {deploy_ref}/rollback` carrying an
   `Idempotency-Key` header (`{deploy_ref}:{run_id}`), persisted with intent
   BEFORE the HTTP call. wololo-deploy MUST make rollback idempotent by that
   key: a replayed key returns the original response and does not execute a
   second rollback. wololo-evals marks the gate `rolled-back` only on a
   confirmed 2xx, never optimistically. A non-2xx (e.g. a transient deploy-side
   5xx) leaves the gate `regressed` with its intent + key persisted and fails
   the durable canary-poll job, which backoff-retries and re-sends the SAME
   key until a 2xx confirms — exactly-once, never at-most-once.

## Test vectors

`test-vectors/<kind>/`: `invalid-` prefixed vectors MUST fail schema
validation, all others MUST pass, each invalid isolating a single violation.
