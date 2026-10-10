# waza front-half spike (slice C)

Question: can `microsoft/waza` run as the front half (execute in Copilot, lint, trigger tests, with/without A/B) with wololo-evals as the back half (mechanical scoring, gate)?

**Recommendation: GO for waza as the front half, on Q1, Q3 and Q5.** Auth (Q1), trigger routing (Q3) and the kill-rate join on waza workspaces (Q5) ran end to end on waza output. Q4 no longer supports the verdict: it is unresolved for test-engineer, because whether the with-skills arm loaded the agent cannot be settled from the artifacts. The remaining build is glue items 5 and 6, with glue items 2 and 3 partly open; the complete list is under Open items. See Q4 and Mock verification.

## Verdicts

| # | Question | Verdict | Evidence (computed this spike) |
|---|---|---|---|
| Q1 | Install, Copilot auth | PASS | waza 0.38.9 installed, checksum verified. Copilot sign-in done. Rerun: `waza models` lists 11 models |
| Q2 | Well-formed (`check`, `spec verify`, `tokens`) | FAIL (waza flags) | find-missing-tests 665/500 tokens; test-driven-development 2213/500; agent file rejected by `waza check`; spec verify 0/1 covered |
| Q3 | Does the skill fire (trigger tests) | PASS (weak) | Two identical runs. `skill.invoked` on 2 of 3 positive prompts, 0 of 3 negatives. Accuracy 83.3%, recall 66.7% |
| Q4 | With/without A/B, 3 trials per arm | UNRESOLVED for test-engineer | Rescored gap +0.0099 (pooled SD 0.0157) cannot be read as the agent's effect until it is settled whether the with-skills arm loaded the agent |
| Q5 | The join (waza output to mutation scorer) | PASS (prototype) | Kill rate computed on each of the 6 full-run workspaces by `grade_kill_rate.py`. Open items: see the complete list under Go / no-go |

Spend (AI Credits, as reported by waza; waza gives no USD figure):

| Run | Runs | AI Credits |
|---|---|---|
| Q3 trigger, run 1 | 1 | 33.33 |
| Q3 trigger, rerun | 1 | 33.24 |
| Q4 pilot (1 trial per arm) | 2 | 6.04 |
| Q4 full (3 trials per arm) | 6 | 18.03 |
| **Total metered** | | **90.64** (sum of the rounded rows; unrounded 90.646) |

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
- The earlier figures (0.3461 vs 0.3291, gap 0.0170, best case 85 of 236) are withdrawn. The original 86 of 236 figure was the no-skill trial 1 under the old scoring, also withdrawn. They scored `tests_generated` alone against the workspace's own `quoting/`, which is not the gate's quantity. The rescored values come from the same six kept workspaces; no model was called to produce them.
- The rescored values are not directly comparable to the 0.2373 baseline in Q5. That baseline scores the shipped tests alone, against the fixture.
- Under the committed scoring, all six rescored trials (three per arm) are above the 0.24 threshold, so the threshold does not separate the arms. The NEUTRAL (100% vs 100%) verdict came from a run scored by the withdrawn grader, so it is not evidence.
- Test files differ between arms: different hashes, and in the pilot workspaces 10 vs 14 test functions. Pilot scores were under the withdrawn grader and are not reported.
- Setup finding (corrected again): the committed `test-engineer.agent.md` fails waza's agent parser. Under executor mock, waza rejects it with `line 11: cannot unmarshal !!str 'Read, W...' into []string`. Earlier wording that it "parses without error" was wrong. The earlier playwright error had the same field shape, and that is why it was not isolated.
- (a) Q4 for test-engineer is UNRESOLVED. Whether the with-skills arm loaded the agent cannot be settled from the artifacts, so Q4 cannot be read as the agent's effect. The earlier INVALID label overstated the evidence.
- Artifacts: the three live with-skills runs have status `passed` and `error_msg` null. The mock run failed on the same file through a path the live run did not hit. The live log has no WARN lines, but whether waza prints the Copilot path's skill-resolution warning at the level in use is unverified. Settling it needs a logged session or a fixed agent, and both need model spend, which is on hold. Withdrawn: the earlier wording that the agent was loaded per the trace, and the claim that the `Skill: test-engineer` line showed resolution. That line is the eval's name, and no trace shows the agent body reached the model. On the Copilot path, `buildSkillSystemMessage` (`internal/execution/copilot.go:1046`) logs a warning and injects nothing when the agent fails to parse, so the run continues without the agent body. The warning does not appear in the saved Q4 log, so this is inference from source. [inference]
- Consequence: Q4 cannot test test-engineer in its published form, so its result says nothing about the agent, either way.

## Q5: the join

Post-step, not a waza-native grader. `docs/spikes/waza/test-engineer/grade_kill_rate.py` runs as a waza `program` grader, with the committed behaviour below:

- Code under test: the pristine fixture copy next to the grader (`fixtures/quoting-service/quoting`), never the workspace's `quoting/`.
- Test set: the shipped tests from that pristine copy, plus `tests_generated/` from the workspace. This is the gate's set in `benchmarks/qe-skill/qe-target.json`.
- Threshold: read from `qe-target.json` (0.24).
- Refusals: an unset or non-directory `WAZA_WORKSPACE_DIR`; any symlink under `tests_generated/`; any file there that is not a plain `test_*.py` module or that shares a shipped name (so no `conftest.py` and no shadowing).
- Exit codes: 0 at or above threshold, 1 below threshold, 2 invalid run or grader failure.
- Output: the full report to `kill_rate.json` in the workspace, written through a temp file and rename. waza's program grader is binary, so the number travels in the sidecar. About 4.5 s per run.

The six rescored Q4 workspaces (commit `6945e68`) were scored before the refusal checks were added. Those checks reject only the conftest and shadowing inputs, which the rescored workspaces did not contain, but the numbers were not re-run with the final grader.

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
2. **Grader entrypoint, partly open.** `grade_kill_rate.py` wraps only the mutation scorer. `run_defect_detection` and `score_qe_rubric` are not wrapped.
3. **Fixed test drop path, partly open.** Held in every scored run in this spike (`tests_generated/test_weight_boundary.py`). Waza does not enforce it; the prompt does.
4. **Keep workspace and timeout.** Done: `--keep-workspace`, grader timeout 900 s.
5. **Ingestion, open.** `gate` reads its own run directories. No converter from waza results JSON to that format.
6. **Name mismatch, open.** Casebook `qe-test-authoring` vs shipped skills. Cause of 0.0833 still open (see Q3).
7. **Constraint, not open.** Fixtures and skill directories must sit inside the spec directory, with no `..` and no absolute paths. A skill directory may hold only parseable agent files. The specs follow this.
8. **Constraint, not open.** The `--task` filter drops trigger tests. Trigger runs need at least one task to execute.

### Open items (complete list)

1. Glue 5: ingestion converter from waza results JSON to gate run dirs. Build.
2. Glue 6: casebook slug or shipped skill. Build. The cause of 0.0833 is open with it.
3. Glue 2: defect and rubric scorers not wrapped for a waza workspace. Build.
4. Glue 3: the test drop path is enforced by the prompt only. Design gap.
5. Q4 agent: UNRESOLVED for test-engineer (see Q4). Decision recorded (parent): leave the agent's `tools:` line unchanged, since a test of the agent must use it as its authors wrote it. Consequence: the asset as published does not load in Copilot, so Q4 cannot test the agent. Evidence gap.
6. Spend: the USD conversion of 90.64 AI Credits is open, and the $10 cap is unverified. Budget.
7. Commit 153c8a1 on `slice-c-qe-gate` is not in this clone, so the Q3 correction is unverified here. Evidence gap.

Closed: glue 1 (kill-rate sidecar) and glue 4 (keep-workspace, timeout).

## Go / no-go

**GO: waza as the front half, on Q1, Q3 and Q5.** Auth, trigger routing and the kill-rate join ran on waza output. Q4 no longer supports the verdict: whether its with-skills arm loaded the agent is unresolved, so it says nothing about the agent's effect. The remaining build is the open items above.

- Q4 reading: withdrawn as evidence about the agent. The measured gap (+0.0099, pooled SD 0.0157) and the threshold result (all six trials above 0.24, three per arm) are real measurements under the committed scoring, but whether the with-skills arm loaded the agent is unresolved, so they cannot be read as the agent's effect. Per the slice-c session, a capable model writes these tests unaided, which is a separate question. Next: a test that uses the agent as published, which needs a decision on the asset.
- Build remaining: the Build items in Open items (glue 5, glue 6, glue 2).
- Spend: 90.64 AI Credits metered across Q3 and Q4. USD conversion is open and the $10 cap is unverified. Model spend is on hold: no more trials.

## Mock verification (no model calls)

Scope: mock proves that specs load, that fixture and skill paths resolve from the spec dir, and that the program grader launches and writes its sidecar. Mock does not prove model behaviour: it writes no tests, and its response echoes the prompt.

`waza run` has no executor flag. The executor comes from the spec `config`, so each committed spec was copied to a scratch dir with `executor: mock`. The committed files were not edited for these runs. `S` is the scratch dir.

```bash
# scratch copy of the test-engineer spec, executor set to mock
cp -r docs/spikes/waza/test-engineer $S/mock-te
sed -i 's/^  executor: copilot-sdk/  executor: mock/' $S/mock-te/eval.yaml
# mock writes no tests; this agent-written suite stands in for the agent's output in the scratch fixture
mkdir -p $S/mock-te/fixtures/quoting-service/tests_generated
cp /tmp/waza-1045558716/tests_generated/test_weight_boundary.py $S/mock-te/fixtures/quoting-service/tests_generated/
```

**Run A: committed test-engineer spec, unchanged agent**

```bash
waza run $S/mock-te/eval.yaml --keep-workspace -v --output-dir $S/mock-out-te
```

```
  Run 1/3... error (0s)
  Run 2/3... error (0s)
  Run 3/3... error (0s)
  Failed: 1
```

The run's `error_msg` in the results JSON reads: `line 11: cannot unmarshal !!str 'Read, W...' into []string`, from `parsing agent ".../test-engineer.agent.md": yaml: unmarshal errors:`.

The committed agent has the same field: `tools: Read, Write, Edit, MultiEdit, Grep, Glob, Bash`. The spec does not load as committed.

**Run B: same spec, `tools:` line removed from the scratch agent copy only**

```bash
sed -i '/^tools: /d' $S/mock-te/agents/test-engineer.agent.md
waza run $S/mock-te/eval.yaml --keep-workspace -v --output-dir $S/mock-out-te2
```

```
Skill: test-engineer
Skill Directories:
  - .../mock-te/agents            (spec-relative: resolves from the spec dir)
  [GRADER] mutation_kill_rate score=1.00 ... kill_rate=0.3856 executed=0.6791   (x3 trials)
Succeeded: 1
Workspace preserved: /tmp/waza-mock-1099200919
```

Only the last workspace is preserved, even with `--keep-workspace`. That workspace holds `quoting/ tests/ tests_generated/ kill_rate.json`. Its sidecar:

```
{"killed":91,"total":236,"kill_rate":0.3855932203389831,"threshold":0.24}
```

The 0.3856 is the same suite scored on the gate's test set in Q4. The grader launched through `uv run --project .` from the repo root, wrote the sidecar, and exited on threshold.

**Run C: committed find-missing-tests spec, executor mock**

```bash
cp -r docs/spikes/waza/find-missing-tests $S/mock-fmt
sed -i 's/^  executor: copilot-sdk/  executor: mock/' $S/mock-fmt/eval.yaml
waza run $S/mock-fmt/eval.yaml --keep-workspace -v --output-dir $S/mock-out-fmt
```

```
Skill Directories:
  - .../mock-fmt/skills           (spec-relative)
Succeeded: 1
Workspace preserved: /tmp/waza-mock-3143906682   (contains quoting/ and tests/ from the spec-relative fixture)
```

Not evidence, and not reported as results: the text grader `names_a_missing_test` passed only because the mock echoes the prompt, which contains "missing test". The trigger line (accuracy 100%) is also not evidence: mock invokes no skill.

**Verdicts**

| Spec | Loads | Skill/fixture paths resolve from spec dir | Grader writes sidecar |
|---|---|---|---|
| find-missing-tests (committed) | yes | yes | not applicable (no program grader) |
| test-engineer (committed) | no: agent parse fails | not reached | not reached |
| test-engineer (scratch, `tools` line removed) | yes | yes | yes |

**Decision recorded (parent):** leave the `tools:` line in `test-engineer.agent.md` unchanged. A test of the agent must use it as its authors wrote it. The removal and list-conversion options were considered and not taken. Under mock, the committed spec does not load, as recorded above.

**Asset portability finding (b):** the ainb-toolkit `test-engineer.agent.md` uses a comma-separated `tools:` line. That line is accepted by Claude Code but does not parse as a waza or Copilot custom agent. Waza rejects it under the mock executor (`cannot unmarshal !!str 'Read, W...' into []string`). So the asset as published does not load in Copilot. Claude Code's acceptance is the parent's finding and is not verified here.

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
