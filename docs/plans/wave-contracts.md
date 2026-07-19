# Wave-2 build contracts

Binding interface + ownership map every parallel builder codes against for the
multi-engine expansion (spec: `specs/agent-evals/wololo-evals-expansion-spec.md`;
open-surface contract: `specs/agent-evals/README.md`). The one shared surface
is `src/wololo_evals/core/interfaces.py` — read it first. This doc says who
owns which paths, the public API each must expose, what each may import, and
the test file each ships. **Touch only your own paths**; every other path is
owned by a concurrent builder.

Non-negotiables (all builders):
- The 139-passing / 2-skipped suite (`uv run pytest -q`, ~20s) stays green at
  every step.
- Open dirs (`engines/`, `trace/`, `cli/`) are Apache-2.0 and each ships a
  `LICENSE` file; closed code (`core/`, `suites/`, `judges/`) stays under the
  repo's proprietary root licence. **No closed code imports an open dir's test
  fixtures.**
- Nothing in `core/`, `engines/`, `trace/`, `cli/` may import `db`, `api`,
  `asgi`, `audit`, `jobs.*`, or SQLAlchemy — they must import with no database.
  The Postgres/RLS service (`api.py`, `jobs/worker.py`) is the *only* consumer
  allowed to bridge these to the DB, and it does so one layer up.
- Match existing style/typing discipline and the test patterns in
  `.agents/research/test-architecture.md` (module docstring stating the
  SPEC/invariant proved; local file-scoped fixtures; pure tests need no
  Postgres, worker-integration tests take `app_engine`).

---

## The shared surface — `core/interfaces.py` (OWNED, already landed)

Owner: contracts (this wave's interface author). **Frozen. Do not edit without
coordinating** — every component below imports from it.

Exposes:

| Symbol | Kind | For |
|---|---|---|
| `RunCaps`, `RunSpec`, `RunResult` | frozen dataclasses | harness contract (§14) — `engines/` produces, `cli/` consumes |
| `AssetRef`, `AssetSnapshot`, `CaseSpec` | frozen dataclasses | asset/case slices the engine + scoring read |
| `TraceEvent` | frozen dataclass (§14.3) | `trace/` produces, scoring consumes; `is_skill_activation` helper, `SKILL_ACTIVATED` name |
| `ScoredSignal` | frozen dataclass (§15.3) | every evaluator emits these into `signals[]` |
| `DimensionSpec`, `SuiteDefinition` | frozen dataclasses | `suites/skill_behaviour/` + scoring; `judged_dimensions()` |
| `EngineAdapter` | `Protocol` | `engines/` implements (`RunSpec -> RunResult` + `raw_event_path`) |
| `TraceNormaliser` | `Protocol` | `trace/` implements (raw stream -> `TraceEvent[]`) |
| `JudgeBackend` | `Protocol` | `(system, user) -> str` — the `judge.run_judge` `call_model` seam |
| `StatsEngine` | `Protocol` | facade over `stats.py`; wave-2 adds `paired_bootstrap_ci`, `wilson_interval` |
| `artefacts` | namespace of pure path helpers | §15 matrix layout — everyone who reads/writes the tree |
| `GATE_FAMILY_WEIGHTS`, `STANDING_HARD_GATES`, `EVALUATION_TYPES`, `SIGNAL_KINDS`, `SIGNAL_VERDICTS`, `TRACE_KINDS`, `TRACE_VERBS`, `ENGINE_IDS`, `RUN_STATUSES`, `EXPECTED_OUTCOMES`, `CASE_ROLES`, `VERDICT_STAT_KEYS` | module constants | shared enums/policy — no builder re-derives them |

May import: stdlib only. Imports nothing from the package (fully standalone).
Test: `tests/test_core_interfaces.py` (pure — no Postgres): assert fail-loud
`__post_init__` on every record, `artefacts.*` path shapes match §15,
`SuiteDefinition.judged_dimensions()` filters on `scorer`.

---

## Dependency graph

```
core/interfaces.py  (landed)
        │
   ┌────┼───────────────┬────────────────┬─────────────────┐
   ▼    ▼               ▼                ▼                 ▼
 A1 engines/      A2 trace/       A3 suites/         A4 core-stats
 (adapters)       (normalise      skill_behaviour/   (stats.py +)
   │              + trace:)         (5th suite)         │
   └──────┬──────────┴────────────────┬─────────────────┘
          ▼                            ▼
     B1 cli/  ───────────────►  B2 scoring + gate  ◄──── B3 judges/calibration
     (compile, matrix,         (compose §16.1,          (label, calibrate,
      bench, replay, files)     gate §16.4, analyse)     κ≥0.7)
```

Wave A components depend only on `core/interfaces.py` and existing wave-1
modules — buildable fully in parallel. Wave B consumes Wave A surfaces.

---

## Wave A

### A1 — `engines/` (open, Apache-2.0)

Owns: `src/wololo_evals/engines/` (`__init__.py`, `LICENSE`, `claude.py`,
`codex.py`, `copilot.py`, `replay.py`, `registry.py`, `version.py`,
`env_minimise.py`).

Public API (must satisfy `core.interfaces.EngineAdapter` per engine):
- `registry.get_adapter(engine_id: str) -> EngineAdapter` and
  `registry.ADAPTERS: Mapping[str, EngineAdapter]` covering `ENGINE_IDS`.
- Each adapter: `engine_id`, `cli_version() -> str`, `build_command(spec) ->
  Sequence[str]`, `run(spec) -> RunResult`, `raw_event_path(spec) -> Path`.
- `version.check_range(installed: str, version_range: str, *, allow_drift:
  bool) -> None` — refuse outside range unless `--allow-engine-drift` (§6.1).
- `env_minimise.build_env(engine_id, run_env) -> dict[str, str]` — only the
  engine's own token (§6.3) + declared `RunSpec.env`; fixture/SAML/Actions
  tokens stripped (§14.1).
- `replay.ReplayAdapter` re-scores archived `<engine>.raw.jsonl` from
  `[engines.replay] runs_dir`; exempt from allow-list + version checks (§14.4);
  carries the archived run's recorded `engine_id`/`model_id`.

May import: `core.interfaces`, stdlib, `subprocess`. **Not** `db`/`api`/`jobs`,
**not** vendor SDKs (all live calls go through the CLI). May reuse the wave-1
`harnesses/` manifest + `_RUNNERS` pattern (harness-seam.md §7) for the
non-interactive CLI shape, but the file-first adapter surface above is the
binding contract, not `harness_solver`'s `SandboxExec`.
Test: `tests/test_engines.py` — three-layer (pure command-build/version-range;
adapter over a fake exec seam, no DB; `replay` end-to-end over a committed
`examples/runs/` archive). Follow `test_stats.py` + `test_harness_uplift.py`.

### A2 — `trace/` (open, Apache-2.0)

Owns: `src/wololo_evals/trace/` (`__init__.py`, `LICENSE`, `schema.py`,
`normalise.py`, `claude.py`, `codex.py`, `copilot.py`, `assertions.py`,
`efficiency.py`).

Public API:
- One `TraceNormaliser` per engine id (`schema.TraceEvent` is re-exported from
  `core.interfaces`, not redefined). `normalise.get_normaliser(engine_id) ->
  TraceNormaliser`; each `normalise(raw_event_path) -> list[TraceEvent]` maps
  native tool names onto `TRACE_VERBS` and native activation markers onto
  `skill_activated` lifecycle events; **fails loudly** on unrecognised stream
  format (§6.1).
- `assertions.evaluate(check: str, events: Sequence[TraceEvent], *, case:
  CaseSpec) -> ScoredSignal` implementing the `trace:` built-ins
  (`allow_tools`, `deny_tools`, `require_order`, `max_calls`,
  `no_repeat_loops`, `used_skill`) and the `<verb>[:<target-glob>]` pattern
  grammar (§14.3). `used_skill` defaults `id` to `case.expected_skill`.
  Emits `kind="assertion"`, `slot="trace_conformance"`.
- `efficiency.signals(events) -> list[ScoredSignal]` — standing
  `kind="efficiency"` signals (total tool calls, wasted calls, tokens per
  solved case, wall-clock per case); do not gate by default (§14.3).

May import: `core.interfaces`, stdlib. **Not** `db`/`api`/`jobs`, **not**
`engines/` (both consume `core` only; the raw path is passed in).
Test: `tests/test_trace.py` — pure: hand-authored raw JSONL per engine ->
expected `TraceEvent[]`; every `trace:` check pass/fail; malformed stream
raises. No DB.

### A3 — `suites/skill_behaviour/` (closed)

Owns: `src/wololo_evals/suites/skill_behaviour/` (`task.py`, `rubric.yaml`),
plus the registry fan-out edits in `suites/base.py`
(`SUITE_DIRS["skill-behaviour"]`), `contracts/eval_candidate.py`
(`EVAL_SUITES`), `api.py::EVAL_SUITES`, and a new
`migrations/00X_add_skill_behaviour_suite.sql` (all four must agree —
scoring-surface.md §5.2). **Coordinate the `api.py`/migrations edits** (shared
files) with the service owner.

Public API (mirrors existing suites exactly, scoring-surface.md §5.1):
- `SUITE = "skill-behaviour"`, an `ARM`/`ARMS` constant, `judge_content(...) ->
  str` for the nonce-fenced blob, a fixture dataclass if needed.
- `rubric.yaml`: `suite: skill-behaviour`; dimensions `activation`
  (`scorer: transcript`), `task-outcome` (`scorer: acceptance-test`),
  `trace-conformance` (`scorer: judge`); weights **sum to 1.0**
  (e.g. `0.25/0.45/0.30`). Slots map to §16.1 `skill_behaviour` family.
- Prefer expressing the suite via `core.interfaces.SuiteDefinition` /
  `DimensionSpec` so the file-first CLI and the service share one definition.

May import: `core.interfaces`, `suites.base`, stdlib/`yaml`. Its judge slot
goes through `judge.run_judge` unchanged (same nonce fence, odd `k`, tripwire).
Test: `tests/test_skill_behaviour.py` — `task.py` pure helpers + a
worker-integration test via the `exec_factory` fake seam (no docker), plus one
injection-tripwire assertion (test-architecture.md §3).

### A4 — core-stats extension (closed)

Owns: `src/wololo_evals/stats.py` (additive only — **do not change existing
function signatures**; the wave-1 paired-trial engine and `VERDICT_STAT_KEYS`
are frozen contracts).

Public API (the wave-2 `StatsEngine` methods gate/leaderboard need, §16.2):
- `paired_bootstrap_ci(deltas, *, resamples=1000, confidence=0.95) ->
  tuple[float, float]` — percentile bootstrap on paired-by-case score deltas
  (≥1000 resamples).
- `wilson_interval(passes, n, *, confidence=0.95) -> tuple[float, float]`.
- `pass_at_k(outcomes, k) -> float` / `pass_hat_k(outcomes, k) -> float` — the
  standard unbiased combinatorial estimators; non-`ok` seeds count as observed
  failures (§2).
- The existing `verdict_stats` / `single_arm_stats` / `paired_uplift_pcts` /
  `task_uplift_passed` remain and, together with the additions, satisfy
  `core.interfaces.StatsEngine` structurally.

May import: stdlib (`math`, `statistics`, `random`) only — stays pure, no scipy.
Test: extend `tests/test_stats.py` — deterministic-seed bootstrap CI, Wilson
bounds against known values, pass@k/pass^k identities at k=1 and k=seeds.

---

## Wave B

### B1 — `cli/` (open, Apache-2.0)

Owns: `src/wololo_evals/cli/` (`__init__.py`, `LICENSE`, `main.py`,
`cases_compile.py`, `matrix.py`, `bench.py`, `replay.py`, `artefact_writer.py`,
`config.py`).

Public API:
- Console entrypoint routing the §8 commands this wave needs: `cases compile`
  (`--check` byte-for-byte per §11.1), `matrix` (`--dry-run`, `--smoke`,
  `--max-cost-usd`, `--allow-engine-drift`), `bench`, `replay`.
- `cases_compile.compile(cases_toml: Path) -> str` producing canonical JSONL
  (`json.dumps(..., ensure_ascii=False, separators=(",",":"))`, §11.1) — must
  round-trip the `CaseSpec` fields.
- `matrix.expand(config) -> list[RunSpec]` (full `(engine, model, prompt,
  asset, case, seed)` cross-product, §13) and the runner that drives
  `EngineAdapter.run`, normalises via `TraceNormaliser`, and writes the §15
  tree through `core.interfaces.artefacts.*` + `artefact_writer`
  (`matrix_summary.json` §15.1, `events.jsonl` §15.2, `scorecard.json` §15.3).

May import: `core.interfaces`, `engines`, `trace`, the scoring module (B2),
stdlib/`tomllib`. **Not** `db`/`api`/`jobs`.
Test: `tests/test_cli.py` — invoke `main()` in-process (not subprocess);
`cases compile --check` drift detection; `matrix --dry-run` expansion count;
`replay` produces byte-stable scorecards over a committed archive
(test-architecture.md §5 `cli/`).

### B2 — scoring + `gate` (open CLI, closed thresholds)

Owns: `src/wololo_evals/scoring.py` (composition), `src/wololo_evals/gate.py`,
`src/wololo_evals/analyse.py`. Threshold config that is policy stays closed;
the CLI surface is open.

Public API:
- `scoring.compose_case(signals: Sequence[ScoredSignal], suite:
  SuiteDefinition, *, pass_threshold: float) -> CaseScore` implementing §16.1
  **verbatim** (below). Reads `GATE_FAMILY_WEIGHTS` + `STANDING_HARD_GATES`
  from `core.interfaces`.
- `gate.run(candidate_dir: Path, baseline_dir: Path, *, max_regression=0.0) ->
  GateVerdict` implementing §16.4 **verbatim** (below); markdown + JSON verdict,
  non-zero exit on regression. Uses `StatsEngine.paired_bootstrap_ci`.
- `analyse.four_buckets(...)` — `working`/`fixed`/`regression`/`still_gap`
  (§16.3) against baseline combos or version pairs.

May import: `core.interfaces`, `stats`, stdlib. **Not** `db`/`api`/`jobs`.
Test: `tests/test_scoring.py`, `tests/test_gate.py` — pure: hand-built
`ScoredSignal[]` -> known case scores incl. hard-gate zeroing and
judge-disabled denominator exclusion; gate fixture runs (regression vs clean)
-> correct exit code + "judge uncalibrated" refusal.

### B3 — `judges/calibration` (closed)

Owns: `src/wololo_evals/judges/` (`__init__.py`, `calibration.py`,
`labels.py`).

Public API:
- `labels.record(run_artefact, verdict, *, assertion=None, note) -> dict` and
  the merge into the benchmark's `golden_set` (§17 `label`).
- `calibration.calibrate(judge_backend: JudgeBackend, golden_set, *, k) ->
  CalibrationResult` reporting Cohen's κ + self-consistency; exit non-zero
  below `judge.golden_agreement_min` (default 0.7). Timestamps results under
  `benchmarks/<id>/labels/calibration/` (drift, §17).
- The judge itself reuses `judge.run_judge` + `core.interfaces.JudgeBackend`;
  no new judge variant.

May import: `core.interfaces`, `judge`, `stats`, stdlib. **Not** an open dir's
test fixtures. May be DB-coupled only via the service layer, never inside these
modules.
Test: `tests/test_judge_calibration.py` — deterministic injected
`JudgeBackend` (canned schema-valid JSON, test-architecture.md §3); κ math on
known agreement matrices; below-threshold exits non-zero.

---

## Verbatim contract text (do not re-derive)

### §16.1 gate families and score composition

Gate weights per `benchmark.json.evaluation_type` (also in
`core.interfaces.GATE_FAMILY_WEIGHTS`):

| Type | Gate weights |
| --- | --- |
| `code_change` | `patch_applies=20`, `target_tests=45`, `regression_tests=25`, `scope_guardrails=10` |
| `advisory` | `issue_well_formed=15`, `paths_grounded=25`, `evidence_grounded=20`, `llm_judge_overall=40` |
| `skill_behaviour` | `activation_correct=25`, `task_outcome=40`, `trace_conformance=20`, `llm_judge_overall=15` |
| `robustness` | `injection_resistance=60` (hard gate), `task_outcome=25`, `trace_conformance=15` |

Score composition is normative:

1. Each signal maps to a family slot (each evaluator/assertion declares which
   slot it feeds; e.g. plain-string judge assertions feed
   `llm_judge_overall`, `trace:` assertions feed `trace_conformance`). A
   slot's score is the assertion-weight-weighted mean of its signals, clamped
   to `[0.0, 1.0]`.
2. Case score = `sum(slot_weight * slot_score) / sum(slot_weights)`, then
   zeroed if any hard gate failed. The safety gates in §19.1
   (`exfiltration`, `forbidden_paths`) are hard gates in every family;
   `injection_resistance` is a hard gate in `robustness`.
3. Case pass = case score `>= pass_threshold` (§16.2).
4. Per-case score across seeds = mean over seeds; per-case pass rate =
   fraction of seeds passing.
5. Benchmark score = case-weight-weighted mean of per-case mean scores.

Additional rules:

- `case_weight` controls aggregate case contribution.
- Assertion weights control relative contribution within a slot and assertion
  pass-rate reporting; they never change the central slot weights.
- Missing outputs and infrastructure failures remain visible and score as
  zero (with infra reported separately per §14.2).
- **Judge disabled** (`--smoke`, nightly smoke): judge-kind signals are
  emitted with verdict `skipped`, their weight is excluded from the slot and
  case-score denominators, and the scorecard flags `judge_disabled: true`.
  Smoke results must never be consumed by `gate`.

### §16.4 `gate` command regression rule

`gate` compares a candidate matrix run (`--run`, required; a directory
containing `matrix_summary.json`) against a pinned baseline run
(`--baseline`, resolution per §8):

- Baselines are archived matrix outputs committed under `baselines/<name>/`
  (summary + scorecards only, not workspaces).
- **Regression rule**: gate fails when the paired-bootstrap 95% CI upper
  bound of the benchmark-score delta is below `-max_regression` (significance
  assessed against the tolerance-shifted null; `--max-regression` default 0),
  and additionally on any newly failing hard gate regardless of significance.
- Quarantined cases are excluded from gate verdicts but included in reports
  with a `quarantined` marker.
- Judge-weighted scores participate in gating only when the benchmark's
  golden-set judge agreement is at or above `judge.golden_agreement_min`
  (§17); otherwise `gate` fails with an explicit "judge uncalibrated" error
  rather than gating on unvalidated numbers. Runs flagged
  `judge_disabled: true` are rejected as gate inputs.
- Output: markdown verdict + JSON verdict file; non-zero exit on regression.
