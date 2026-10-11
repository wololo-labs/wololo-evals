# Adversarial review of the whole repository, October 2026

Scope: all of `main` at f0d0b7f. Three passes: `codex review` over the whole tree
(base = root commit), one adversarial codex run per area (engines, scoring,
matrix and artefact writer, gate and statistics, trace normalisers, server,
docs against code), and a read of the same areas by hand. The question put to
each: what makes a verdict wrong, gameable or silently optimistic, and what is
missing that a gate of record needs.

The whole-tree `codex review` reported no actionable defects. It is a diff
reviewer and its prompt was "all of main"; it found nothing the area runs did
not, and it found no bug the area runs missed, so it is listed here only as a
negative result.

Every row below was checked against the code before it was acted on. Status is
`fixed (commit)`, `rebutted (evidence)` or `follow-up` (low, or blocked on
something this review cannot do offline). No model was run and nothing was spent.

## Results that the fixes could affect

None of the published verdicts moves. The four Skerry gate pairs and the
test-writer-pack pairs were replayed through the fixed `gate` from their stored
run trees: verdicts and mean deltas are identical, and the Skerry +0.1238 stands.

Two scorer changes would change a recomputed score, and are flagged rather than
rerun:

- **Result affected, rerun needed (`qe_flake_resistance`).** A suite whose tests
  fail on every seed used to score 1.0 and now scores below it. Stored scorecards
  contain such suites: test-writer-pack forced arm case `qe-02-destination-zones`,
  the ledger baseline cases `qe2-01` to `qe2-04`, and the Skerry calibration case
  `qe3-07` (not evidence). The test-writer-pack forced verdict was already a
  regression on `mutation_kill_rate` and `seeded_defects_caught`; the correction
  can only lower that arm further. The ledger baseline numbers are the ones
  already withdrawn in `docs/QE-TEST-AUTHORING-CONCLUSION.md`.
- **Result affected, rerun needed (`qe_assertion_strength`).** `pytest.raises(Exception)`
  no longer counts as strong. Two retained workspaces use it (the withdrawn
  ledger baseline `qe2-04` and the Skerry calibration case `qe3-05`).

Token totals in stored traces were computed with the old addition and are not
recomputed. Cost and the verdicts above do not depend on them.

## Findings

| id | area | severity | claim | evidence | status |
|---|---|---|---|---|---|
| E-1 | engines | high | `--allow-engine-drift` never reaches the adapter's version check | `run_matrix` took the flag and `_spec_extra` never emitted it; `_base.py:204` reads it from `spec.extra`. Reproduced: spy adapter saw `None` | fixed (b208cd2) |
| E-2 | engines | medium | A trial can run four times and `attempts` records only the last call | adapter retry plus `_run_with_retry` retry; count came from the second call. Reproduced with a flaky adapter returning 2 and 2 | fixed (6723ecb) |
| E-3 | engines | medium | Unknown trace events are skipped and the run is still graded | same as N-1 | fixed (10484eb) |
| S-1 | scoring | high | `apply_defect` joins `defect.file` to the root unchecked, so `../x` rewrites a file outside the workspace | reproduced with a `../outside.py` manifest; file changed | fixed (685c45c). Trust note: manifests are repo-authored, not agent-authored, so this is hardening |
| S-2 | scoring | high | Changed-line coverage ignored the pytest exit code | `qe_rubric.py` ran `coverage run` and discarded the return value | fixed (2a21f01): exit 1 recorded as `suite_failed`, any other non-zero raises |
| S-3 | scoring | medium | A suite that fails on every seed scores 1.0 flake resistance | identical outcomes across seeds counted as stable; reproduced | fixed (ca4b782). Result affected, see above |
| S-4 | scoring | medium | Out-of-range changed lines were warned about and the score came from the rest | reproduced: lines `[1, 2, 3, 40]` returned 1.0 with a warning | fixed (fe7ccb3): now raises, reported by the grader as `evaluator_error` |
| S-5 | scoring | medium | `pytest.raises(Exception)` counts as a strong assertion | `_raises_blocks` ignored the exception type; reproduced | fixed (1d8de53). Result affected, see above |
| M-1 | matrix | critical | Provenance says `hermetic: true` though trials are not sandboxed | `matrix.py` wrote it; the workspace comment says an absolute path still works | fixed (de0791f). Rated critical by the reviewer for the false claim; the isolation itself is unchanged and still not a sandbox |
| M-2 | matrix | high | A missing asset path becomes an empty asset, and every directory asset shares one digest | `_snapshot`: no `else` branch; digest hashed `body` only, empty for directories. Reproduced both | fixed (51a965d). Directory digests now differ from the old scheme for any future run |
| M-3 | matrix | high | Reusing an output timestamp overwrites a finished run and can leave its old summary beside new files | `mkdir(exist_ok=True)`; reproduced | fixed (bcaa201) |
| M-4 | matrix | high | Live seeds label trials but no adapter passes them to the model | `spec.seed` is read only by the replay adapter. True and unavoidable: the CLIs expose no sampling seed | follow-up (low): say in the docs that a live seed is a repeat index, not a random seed |
| M-5 | matrix | high | A zero or negative `case_weight` can push the weighted score above 1 | `artefact_writer.py:313` divides by the weight sum; reproduced with weights 1 and -0.5 giving 2 | fixed (2f1d02d) |
| M-6 | matrix | medium | `run_matrix` can run stale compiled cases | it read `cases.jsonl` without the `cases compile --check` comparison; reproduced | fixed (b724699) |
| M-7 | matrix | medium | `seeds = 0` or negative passes config loading and divides by zero later | reproduced | fixed (1256240) |
| M-8 | matrix | medium | The matrix cost cap is checked between cases, not during one | true. Cost is only known after a run ends, so a hard per-run cap needs an engine-side budget flag. Passing the remainder as a post-hoc cap would turn a paid run into `cost_cap_hit` | follow-up: needs engine support |
| G-1 | gate | high | Baseline cases the candidate omits are dropped silently | the pairing loop walked candidate cases only; reproduced: candidate missing the failing case passed | fixed (5e5dc8f); `--allow-subset` to compare the overlap on purpose |
| G-2 | gate | high | Combos match on engine, model and prompt only, so different benchmarks or bars compare | reproduced with changed `evaluation_type`, `pass_threshold`, `benchmark_id` | fixed (14f36b1). `benchmark_id` is compared only when both runs declare one, because archived baselines derive it from the directory name |
| G-3 | gate | high | Only the candidate's `case_weight` enters the bootstrap | `gate.py` read `cand_case["case_weight"]` alone; reproduced: weight 50 on one improving case | fixed (c50820f): differing weights refuse |
| G-4 | gate | high | A hard-gate signal the candidate drops is not a hard failure | `_case_hard_failed` only recognised an explicit fail; reproduced | fixed (54613b8) |
| G-5 | gate | high | The bootstrap accepts confidence 0 or 1, NaN and non-positive weights | reproduced for each | fixed (0bc1411) |
| G-6 | gate | high | No minimum number of comparable cases | n = 1 reached the bootstrap and could pass or fail | fixed (67b39af): default floor 3, `--min-cases`. The published Skerry and test-writer gates have 3 or more cases and replay unchanged |
| G-7 | gate | medium | An unknown signal verdict is averaged in with a missing score read as 0.0 | reproduced | fixed (3596011) |
| G-8 | gate | medium | Duplicate case ids keep only the last row | reproduced | fixed (3596011) |
| G-9 | gate | medium | A partial evaluator outage on a guarded signal leaves the guard looking healthy | rebutted in part: errored rows are excluded from the mean by design (`test_a_scorer_outage_on_one_arm_is_not_a_regression`, `gate.py` `_signal_means`), and the count is carried as `candidate_errors` in the signal table. An all-error arm already refuses | follow-up (low): print a note when a guarded signal has any errored rows |
| G-10 | gate | medium | `ab.compare` returns a neutral verdict when every case is quarantined | the CLI refused before calling it; a direct caller did not | fixed (0bc1411) |
| N-1 | trace | high | Unknown events can hide a disallowed call from `deny_tools` while the run still scores | the PR #5 change skipped them and recorded counts per case only | fixed (10484eb): counts roll up to provenance and `gate` refuses unless `--allow-skipped-trace` |
| N-2 | trace | high | A rejected `Skill` call counts as an activation | emitted from `tool_use` alone; reproduced with `is_error` | fixed (b668109). A stream cut before the result keeps the activation |
| N-3 | trace | medium | Copilot tool arguments that are not an object become `{}` | reproduced with list, number and string | fixed (1f34377) |
| N-4 | trace | medium | Token reports are added in full | recorded Claude run: normalised sum 50 in / 8162 out against the CLI's total 14 / 8119 | fixed (49ca7e3). Codex and Copilot cumulative semantics are the stated contract; no recorded fixture exists to check them against a live stream |
| N-5 | trace | low | Skip counts persist across streams on the shared normaliser | reproduced | fixed (5f152fb) |
| V-1 | server | high | A submitter can make the API read any host repository through `repo_path` | `api.py` passed it to `repo_gen.generate` unchecked; reproduced | fixed (76b09f1): needs `WOLOLO_EVALS_REPO_EVAL_ROOTS`, empty by default, so repo-eval over the API is off until an operator sets it |
| V-2 | server | high | The canary gate accepts a smoke run | `open_gate` never read `runs.smoke`; reproduced | fixed (4d9d1fd) |
| V-3 | server | high | The gate records the caller's candidate, not the run's | reproduced | fixed (4d9d1fd) |
| V-4 | server | high | Audit rows are mutable by the service roles | `001_init.sql` granted update on every table | fixed (3ec7342): migration 004 revokes update, delete, truncate on `audit_events`. The head row stays updatable by design; an app-role rewrite of the head alone does not forge events and the chain recompute still checks every row |
| V-5 | server | medium | Keyring writes lock threads only | six forked processes lost keys and crashed on the shared temp file; reproduced | fixed (a099c41) |
| D-1 | docs | high | "DDL-enforced" five-trial floor, and the contract schema accepts `n_trials` of 1 | the DDL check is on the tenant default; the floor on a run is applied in `api.py:248` and smoke runs may go below it | fixed (709ee55) in the docs. The schema is not tightened: a valid smoke run has fewer than five trials and the schema has no smoke field |
| D-2 | docs | high | The first telemetry window is the stable baseline | ingestion freezes whichever arrives first | fixed (6ef2c5e) in the docs |
| D-3 | docs | high | Rollback is exactly-once across crashes | the service re-sends the same key; the single effect depends on the deploy side | fixed (f2ce597) in the docs |
| D-4 | docs | high | The crash proof shows one rollback | it shows one with a stub that de-duplicates | fixed (f2ce597) in the docs |
| D-5 | docs | medium | The interval is Student-t | beyond df 30 the code used 1.960; t(31) is 2.040 | fixed (2774aba) in the code |
| D-6 | docs | medium | Invalid vectors prove the five-trial floor | the vectors cover schema violations only; the floor is not in the schema | fixed (709ee55), same wording change as D-1 |
| D-7 | docs | low | The spec names `core/contracts/...` paths that do not exist | rebutted: `specs/` is published from the kernel repo and `docs/INTEGRATION-DEBT.md:10` says the paths belong to it | rebutted |

## Found while verifying

- `tests/test_worker.py::test_run_once_dispatches_from_queue` fails with
  `InvalidTag` when run together with `tests/test_api.py`, on `main` as well as on
  this branch, and passes alone and in the full-suite order. It is a test
  isolation fault around the shared keyring, not a product fault. Follow-up (low).
- The README row for `qe_flake_resistance` described "outcome holds across hash
  seeds"; it now also requires the test to pass.

## Follow-ups (low or blocked)

1. M-4: document that a live seed is a repeat index.
2. M-8: a per-run cost ceiling needs the engine's own budget flag.
3. G-9: print a note when a guarded signal has errored rows.
4. Test isolation fault above.
5. N-4: record one live Codex and one live Copilot stream to pin their token-report semantics.
