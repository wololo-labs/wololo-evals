# waza front-half spike (slice C)

Question: can `microsoft/waza` run as the front half (execute in Copilot, lint, trigger tests, with/without A/B) with wololo-evals as the back half (mechanical scoring, gate)?

**Recommendation: GO for waza as the front half.** Every front-half function ran end to end: auth (Q1), lint (Q2), trigger routing (Q3), paired with/without execution (Q4), and the kill-rate join on waza workspaces (Q5). The remaining build is glue items 5 and 6. The Q4 result (no detectable skill effect at 3 trials per arm) is a fixture limit, not a waza limit. See Go / no-go.

## Verdicts

| # | Question | Verdict | Evidence (computed this spike) |
|---|---|---|---|
| Q1 | Install, Copilot auth | PASS | waza 0.38.9 installed, checksum verified. Copilot sign-in done. Rerun: `waza models` lists 11 models |
| Q2 | Well-formed (`check`, `spec verify`, `tokens`) | FAIL (waza flags) | find-missing-tests 665/500 tokens; test-driven-development 2213/500; agent file rejected by `waza check`; spec verify 0/1 covered |
| Q3 | Does the skill fire (trigger tests) | PASS (weak) | Two identical runs. `skill.invoked` on 2 of 3 positive prompts, 0 of 3 negatives. Accuracy 83.3%, recall 66.7% |
| Q4 | With/without A/B, 3 trials per arm | DONE, no detectable skill effect | With skills mean kill rate 0.3785, without 0.3686, gap +0.0099, pooled SD 0.0157 (gate's test set, rescored). waza verdict NEUTRAL (100% vs 100%) |
| Q5 | The join (waza output to mutation scorer) | PASS (prototype) | Kill rate computed on each of the 6 full-run workspaces by `grade_kill_rate.py`. Four glue items still open |

Spend (AI Credits, as reported by waza; waza gives no USD figure):

| Run | Runs | AI Credits |
|---|---|---|
| Q3 trigger, run 1 | 1 | 33.33 |
| Q3 trigger, rerun | 1 | 33.24 |
| Q4 pilot (1 trial per arm) | 2 | 6.04 |
| Q4 full (3 trials per arm) | 6 | 18.03 |
| **Total metered** | | **90.65** |

The earlier Q4 pilot, whose with-skills arm failed on agent parsing, was not metered in this total. Failed setup attempts errored at 0 s and cost nothing. The $10 USD cap cannot be checked from these numbers.

## Shape of the join

```
┌───────────────┐  --keep-workspace  ┌──────────────────────┐
│ waza run      │───────────────────▶│ workspace/           │
│ copilot-sdk   │  program grader    │ quoting/ tests_gen/  │
└───────────────┘  (grade_kill_rate) └──────────┬───────────┘
                                                │ kill_rate.json
                                                ▼
                                     ┌──────────────────────┐
                                     │ mutation scorer      │
                                     │ kill_rate (computed) │
                                     └──────────────────────┘
```

## Q1: install and auth

- `install.sh` read first, then run. Installed `waza 0.38.9` to `~/bin/waza`. Checksum verified against the release.
- waza's embedded Copilot CLI `1.0.85` (`~/.cache/copilot-sdk/1.0.85/copilot`) is the one that authenticates. The standalone CLI was installed separately: `npm install -g @github/copilot@1.0.94`.
- Before sign-in, `waza models` failed with:

```
Error: failed to list models: JSON-RPC Error -32603: Request models.list failed with message: GenericFailure, {"kind":"http","status":403,"statusText":"Forbidden","body":"unauthorized: not authorized to use this Copilot feature\n", ...}
```

- The login flow tried to open a browser on this headless box (`xdg-open` missing). The OAuth callback was completed through the local listener on `127.0.0.1`. After sign-in, `waza models` succeeded and lists 11 models.
- Live model IDs on this account include `claude-sonnet-5.5`, `claude-haiku-5.5`, `claude-opus-5.5`. The `claude-sonnet-4.6` ID in the first draft of the spec was not on the account, so the spec uses `claude-sonnet-5.5`.

## Q2: well-formed

| Target | Command | Flags from waza |
|---|---|---|
| find-missing-tests | `waza check skills/find-missing-tests` | Token budget 665 / 500. Unknown frontmatter `user-invocable`. No eval.yaml. No examples section. Scope signal below minimum |
| test-driven-development | `waza check skills/test-driven-development` | Token budget 2213 / 500. Unknown frontmatter `argument-hint`, `user-invocable`, `version`. No examples section |
| test-engineer agent | `waza check agents/test-engineer.agent.md` | Rejected: `waza check` only accepts skill directories |
| test-engineer agent | `waza tokens count agents/test-engineer.agent.md` | 2026 tokens (no budget applied) |
| find-missing-tests | `waza spec verify <skill> docs/spikes/waza/find-missing-tests/eval.yaml` | 0/1 covered. Uncovered: "create GitHub issues for each gap" |

The 500-token budget is waza's default. Read the flags as "waza's default gate fails", not "the skill is broken".

The spec files validate against waza's published `eval.schema.json` and `task.schema.json` (checked with `jsonschema`). Spec verify's one miss is deliberate: the find-missing-tests task forbids filing issues, because the skill would otherwise write to GitHub.

## Q3: does it fire

Run: `trigger_tests.yaml` from `docs/spikes/waza/find-missing-tests/` (3 positive, 3 negative prompts), with `inject_skill_body: false`. Trigger discovery needs at least one task, and the `--task` filter drops every test, so the run used a scratch spec with a no-tool carrier task. The carrier task's own grader failed (`names_a_missing_test`); that score is an artifact and is not reported.

| Prompt | Expected | Skill invoked | Result |
|---|---|---|---|
| Find the missing test cases in this codebase and file an issue for each gap | fire | yes (`skill.invoked`) | pass |
| What tests are we missing for the payments module? | fire | no (tools used, skill not invoked) | FN |
| Audit our test coverage gaps and open GitHub issues for them (medium) | fire | yes | pass |
| Fix the failing login test in auth.spec.ts | silent | no | pass |
| What's the weather like in Lisbon today? | silent | no | pass |
| Deploy the staging build to Azure | silent | no | pass |

- Two runs gave the same metrics: TP 2, FP 0, FN 1, TN 3, so accuracy 83.3% (5/6), precision 100%, recall 66.7% (2/3), F1 80%. The earlier 81.8%, 60% and 75% were wrong and are withdrawn. Six prompts is still a small sample.
- Evidence is `skill.invoked` events in the run transcript, not response text.
- Our 0.0833 activation number is not in this repo (no run artifact, no matching value). Source unverified.
- 0.0833 = 1/12, which matches 12 cases with one hit. [inference]
- The casebook asserts `trace:used_skill(qe-test-authoring)` (`benchmarks/qe-skill/cases.toml:31,57`). The pack ships `expect-test`, `find-missing-tests`, `test-driven-development`, `webapp-testing`. `qe-test-authoring` is not in the pack, on any branch (`git log --all -S`), or in GitHub code search. [fact]
- STALE hypothesis: the scored name `qe-test-authoring` does not exist, so no shipped skill can satisfy it. This came from reading `main` only. Parent correction: unpushed commit 153c8a1 on `slice-c-qe-gate` ("score activation against the asset under evaluation") makes the assertion fall back to each case's `expected_skill`, and a matrix config overrides it per arm. That commit is not in this clone, so it is unverified here.
- Cause of 0.0833: OPEN. The waza trigger-test runs above are the evidence now.
- Repo copilot adapter has never run live (`docs/INTEGRATION-DEBT.md:26`). [fact]

## Q4: A/B

Spec: `docs/spikes/waza/test-engineer/eval.yaml`. Task: write pytest tests for the `weight_grams` boundary in `quoting/`, into `tests_generated/`. Model `claude-sonnet-5.5`, `--baseline --trials 3 --keep-workspace`. The task forbids touching `quoting/` and filing issues.

Per-trial mutation kill rate on the gate's own terms (shipped tests plus `tests_generated`, scored against the pristine fixture), from the fixed `grade_kill_rate.py` on each kept workspace. Computed. Best case is 92 of 236 mutants killed (no-skill trial 1, 0.3898).

| Trial | With skills | Without skills |
|---|---|---|
| 1 | 0.3644 (86/236) | 0.3898 (92/236) |
| 2 | 0.3856 (91/236) | 0.3559 (84/236) |
| 3 | 0.3856 (91/236) | 0.3602 (85/236) |
| **Mean** | **0.3785** (SD 0.0122, range 0.0212) | **0.3686** (SD 0.0185, range 0.0339) |

- Gap, with minus without: **+0.0099**. Pooled SD 0.0157. The gap is smaller than the pooled SD and smaller than either arm's range. At 3 trials per arm this run cannot tell the arms apart. [fact]
- The earlier figures (0.3461 vs 0.3291, gap 0.0170, best case 85) are withdrawn. They scored `tests_generated` alone against the workspace's own `quoting/`, which is not the gate's quantity. The rescored values come from the same six kept workspaces; no model was called to produce them.
- The rescored values are not directly comparable to the 0.2373 baseline in Q5. That baseline scores the shipped tests alone, against the fixture.
- waza's own binary grader passed 6 of 6 runs at the 0.24 threshold, and waza reports NEUTRAL (100% vs 100%). Its own metric gives no signal here, so the kill rate is the metric that carries the comparison.
- Test files differ between arms: different hashes, and in the pilot workspaces 10 vs 14 test functions. Pilot scores were under the withdrawn grader and are not reported.
- Setup finding (corrected): an earlier with-skills arm errored once, when the shared ainb-toolkit agents dir was used. The error was a YAML unmarshal failure on `playwright-test-generator.agent.md`. The cause is not established. `test-engineer.agent.md` has the same comma-separated `tools` shape and parses without error: the final run log has no parse warnings and prints `Skill: test-engineer`. The playwright file's exact failing field was not isolated, and that log was overwritten.
- Was test-engineer loaded in the with-skills arm? Evidence it was resolved: `Skill: test-engineer` printed and no parse warning appeared, so waza had the agent definition. Not shown: a per-session record that the agent body reached the model. The results digest holds only bash tool calls and names no skill. So Q4 cannot fully separate "not loaded" from "no effect". The resolution evidence points to loaded, but that is not proof.

## Q5: the join

Post-step, not a waza-native grader. `docs/spikes/waza/test-engineer/grade_kill_rate.py` runs as a waza `program` grader. It reads `WAZA_WORKSPACE_DIR`, scores `tests_generated/` against `quoting/` with `run_mutation_analysis`, writes the full report to `kill_rate.json` in the workspace, and exits 0 at or above 0.24. waza's program grader is binary, so the number travels in the sidecar. The grader ran in about 4.5 s per run.

Baseline on the fixture with its own shipped tests (no waza):

```
run_mutation_analysis('examples/fixtures/quoting-service/quoting', ['examples/fixtures/quoting-service/tests'])
killed 56 · survived 78 · no_tests 102 · total 236
kill_rate 0.2373 (strict) · kill_rate_executed 0.4179 · ~5 s
```

- The shipped suite scores 0.2373 against `mutation_threshold` 0.24 in `benchmarks/qe-skill/qe-target.json`. It fails that threshold by 0.003. [fact]
- Waza facts: the `program` grader receives the agent output on stdin and `WAZA_WORKSPACE_DIR`, and returns binary pass/fail (`docs/graders/program.md`). Results JSON records `workspace_dir` (`internal/models/outcome.go:215`). [fact]

### Glue status

1. **Numeric score path.** Done in the prototype: `kill_rate.json` in the workspace.
2. **Grader entrypoint.** Prototype exists (`grade_kill_rate.py`). Not yet a wrapper for `run_defect_detection` or `score_qe_rubric`.
3. **Fixed test drop path.** Held in every scored run in this spike (`tests_generated/test_weight_boundary.py`). Waza does not enforce it; the prompt does.
4. **Keep workspace and timeout.** Done: `--keep-workspace`, grader timeout 900 s.
5. **Ingestion, open.** `gate` reads its own run directories. No converter from waza results JSON to that format.
6. **Name mismatch, open.** Casebook `qe-test-authoring` vs shipped skills. Cause of 0.0833 still open (see Q3).
7. **New: spec layout rules.** Fixtures and skill directories must sit inside the spec directory, with no `..` and no absolute paths. A skill directory may hold only parseable agent files.
8. **New: `--task` filter drops trigger tests.** Trigger runs need at least one task to execute.

## Go / no-go

**GO: waza as the front half.** The spike question is whether waza runs the front half, and it does. The remaining build is glue.

- Q4 reading: no detectable skill effect at 3 trials per arm (with 0.3785, without 0.3686, gap 0.0099, pooled SD 0.0157). The no-skill arm also passed waza's binary threshold 6 of 6, so this fixture does not separate the arms. This is a fixture limit, not a waza limit. Per the slice-c session, a capable model writes these tests unaided. The next work is a harder fixture where the no-skill arm fails, not more trials on this one.
- Build remaining: glue item 5 (ingestion converter from waza results JSON to gate run dirs) and glue item 6 (rename the casebook `qe-test-authoring` slug or ship the skill). Item 6 is tied to the open Q3 cause.
- Spend: 90.65 AI Credits metered across Q3 and Q4. USD conversion is open and the $10 cap is unverified. Model spend is on hold: no more trials.

## Reproduce

```bash
# Q1: install (reviewed script; checksum verified by the script)
bash install.sh                       # from raw.githubusercontent.com/microsoft/waza/main
~/bin/waza --version                  # waza version 0.38.9
npm install -g @github/copilot@1.0.94 # standalone Copilot CLI, interactive login follows
copilot login                         # interactive; needs a browser-enabled terminal
~/bin/waza models                     # auth probe: lists models when signed in

# Q2: static checks, no model spend
Q=/home/claude/.orca/ainb-toolkit/packages/qe-agent-pack/.apm
waza check $Q/skills/find-missing-tests
waza check $Q/skills/test-driven-development
waza tokens count $Q/agents/test-engineer.agent.md
waza spec verify $Q/skills/find-missing-tests docs/spikes/waza/find-missing-tests/eval.yaml

# Q3: trigger tests. --task filter drops all tests, so use a scratch spec dir
#     with trigger_tests.yaml, eval.yaml and a no-tool carrier task (tasks/noop.yaml).
waza run <scratch-spec>/eval.yaml -v --output-dir <out>

# Q4 + Q5: A/B on the test-engineer task, grader writes kill_rate.json per workspace
waza run docs/spikes/waza/test-engineer/eval.yaml --baseline --trials 3 --keep-workspace -v --output-dir <out>

# Q5 baseline: scorer on the fixture (venv with the mutation extra)
uv venv <venv> && uv pip install --python <venv>/bin/python -e '.[mutation]'
<venv>/bin/python -c "from wololo_evals.scoring.mutation import run_mutation_analysis as r; x=r('examples/fixtures/quoting-service/quoting',['examples/fixtures/quoting-service/tests']); print(x.as_dict(), x.kill_rate)"
```

Clone of `ainb-toolkit` used: `/home/claude/.orca/ainb-toolkit` at `a3aaa7e`.
