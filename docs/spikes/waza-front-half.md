# waza front-half spike (slice C)

Question: can `microsoft/waza` run as the front half (execute in Copilot, lint, trigger tests, with/without A/B) with wololo-evals as the back half (mechanical scoring, gate)?

**Recommendation: HOLD. Front half is partly proven, A/B is not run.** Activation works on find-missing-tests. Q4 (A/B) is the open gate. The back half works.

## Verdicts

| # | Question | Verdict | Evidence (short) |
|---|---|---|---|
| Q1 | Install, Copilot auth | PASS | waza 0.38.9 installed, checksum verified. Copilot 403 cleared after sign-in. `waza models` lists 11 models |
| Q2 | Well-formed (`check`, `spec verify`, `tokens`) | FAIL (waza flags) | find-missing-tests 665/500 tokens; test-driven-development 2213/500; agent file rejected by `waza check`; spec verify 0/1 covered |
| Q3 | Does the skill fire (trigger tests) | PASS (weak) | `skill.invoked` on 2 of 3 positive prompts, 0 of 3 negatives. One trial, six prompts. Accuracy 81.8% |
| Q4 | With/without A/B, trials 3 | NOT RUN | Spec not finalised. Cost cap not agreed. No scores computed, none reported |
| Q5 | The join (waza output to mutation scorer) | BLOCKED (scorer side PASS) | Scorer computes kill_rate 0.2373 on the fixture with its own tests. No waza run output exists to feed it yet |

Spend: one Q3 run, 7 premium requests, 33.33 AI Credits as reported by waza. Waza reports no USD figure, so the $10 budget cannot be checked from its output.

## Shape of the join

```
┌───────────────┐  --keep-workspace  ┌──────────────────────┐
│ waza run      │───────────────────▶│ workspace/           │
│ copilot-sdk   │  Q3 ran; Q4 next   │ quoting/ tests_gen/  │
└───────────────┘                    └──────────┬───────────┘
                                                │ GLUE MISSING
                                                ▼
                                     ┌──────────────────────┐
                                     │ mutation scorer      │
                                     │ kill_rate (PASS)     │
                                     └──────────────────────┘
```

## Q1: install and auth

- `install.sh` read first, then run. Installed `waza 0.38.9` to `~/bin/waza`. Checksum verified against the release.
- waza's embedded Copilot CLI `1.0.85` (`~/.cache/copilot-sdk/1.0.85/copilot`) is the one that authenticates. The standalone CLI was installed separately: `npm install -g @github/copilot@1.0.94`.
- Before sign-in, `waza models` failed with:

```
Error: failed to list models: JSON-RPC Error -32603: Request models.list failed with message: GenericFailure, {"kind":"http","status":403,"statusText":"Forbidden","body":"unauthorized: not authorized to use this Copilot feature\n", ...}
```

- The login flow tried to open a browser on this headless box (`xdg-open` missing). The OAuth callback was completed through the local listener on `127.0.0.1`. After sign-in, `waza models` succeeded.
- Live model IDs on this account include `claude-sonnet-5.5`, `claude-haiku-5.5`, `claude-opus-5.5`. The `claude-sonnet-4.6` ID in the first draft of the spec was not on the account, so the spec was corrected to `claude-sonnet-5.5`.

## Q2: well-formed

| Target | Command | Flags from waza |
|---|---|---|
| find-missing-tests | `waza check skills/find-missing-tests` | Token budget 665 / 500. Unknown frontmatter `user-invocable`. No eval.yaml. No examples section. Scope signal below minimum |
| test-driven-development | `waza check skills/test-driven-development` | Token budget 2213 / 500. Unknown frontmatter `argument-hint`, `user-invocable`, `version`. No examples section |
| test-engineer agent | `waza check agents/test-engineer.agent.md` | Rejected: `waza check` only accepts skill directories |
| test-engineer agent | `waza tokens count agents/test-engineer.agent.md` | 2026 tokens (no budget applied) |
| find-missing-tests | `waza spec verify <skill> docs/spikes/waza/find-missing-tests/eval.yaml` | 0/1 covered. Uncovered: "create GitHub issues for each gap" |

The 500-token budget is waza's default. Read the flags as "waza's default gate fails", not "the skill is broken".

The spec files validate against waza's published `eval.schema.json` and `task.schema.json` (checked with `jsonschema`). Spec verify's one miss is deliberate: the A/B task forbids filing issues, because the skill would otherwise write to GitHub.

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

- Trigger accuracy 81.8%, precision 100%, recall 60%, F1 75%. TP 2, FP 0, FN 1, TN 3. [fact, one trial]
- Evidence is `skill.invoked` events in the run transcript, not response text.
- Six prompts and one trial is weak evidence. Rerun with more trials before treating recall as settled.
- Our 0.0833 activation number is not in this repo (no run artifact, no matching value). Source unverified.
- 0.0833 = 1/12, which matches 12 cases with one hit. [inference]
- The casebook asserts `trace:used_skill(qe-test-authoring)` (`benchmarks/qe-skill/cases.toml:31,57`). The pack ships `expect-test`, `find-missing-tests`, `test-driven-development`, `webapp-testing`. `qe-test-authoring` is not in the pack, on any branch (`git log --all -S`), or in GitHub code search. [fact]
- Most likely cause of 0.0833: the scored skill name does not exist, so no shipped skill can satisfy the assertion. [inference]
- Falsify: rename the casebook assertion to a shipped skill and rerun. Activation moves, or it stays 0.
- Repo copilot adapter has never run live (`docs/INTEGRATION-DEBT.md:26`). [fact]

## Q4: A/B

Not run. Open items:

- Target: recommend the `test-engineer` agent, not find-missing-tests. find-missing-tests files GitHub issues, a side effect the A/B should not cause.
- Cost: each A/B trial is an agent run with tests written and executed. Waza reports AI Credits, not USD. A cap is needed before running 6 trials (2 arms x 3).

## Q5: the join

Scorer side, run on the fixture with its own shipped tests (no waza):

```
run_mutation_analysis('examples/fixtures/quoting-service/quoting', ['examples/fixtures/quoting-service/tests'])
killed 56 · survived 78 · no_tests 102 · total 236
kill_rate 0.2373 (strict) · kill_rate_executed 0.4179 · ~5 s
```

- The shipped suite scores 0.2373 against `mutation_threshold` 0.24 in `benchmarks/qe-skill/qe-target.json`. It fails that threshold by 0.003. [fact]
- Waza facts: the `program` grader receives the agent output on stdin and `WAZA_WORKSPACE_DIR`, and returns binary pass/fail (`docs/graders/program.md`). Results JSON records `workspace_dir` (`internal/models/outcome.go:215`). [fact]

### Glue missing

1. **Numeric score path.** Program grader is binary. The grader must write the kill rate to a sidecar JSON inside `$WAZA_WORKSPACE_DIR`.
2. **Grader entrypoint.** No module wraps `run_mutation_analysis`, `run_defect_detection` or `score_qe_rubric` for a waza workspace. Needs a small `wololo_evals` grader script.
3. **Fixed test drop path.** The agent must write tests to the path in `qe-target.json` (`tests_generated`). Waza enforces nothing here. Prompt-only, untested.
4. **Keep workspace and timeout.** Run with `--keep-workspace`. Set grader `timeout` to at least 600 s (the default is 30 s, and the scorer defaults to 600 s).
5. **Ingestion.** `gate` reads its own run directories. No converter from waza results JSON to that format.
6. **Name mismatch.** Casebook `qe-test-authoring` vs shipped skills. Rename the casebook or ship the skill.

## Go / no-go

- **HOLD.** Activation works on find-missing-tests (Q3, weak). A/B is unproven (Q4 not run).
- Back half is ready: the scorer computes a real kill rate; the glue is six items, none a blocker to write.
- Next: agree a cost cap, run Q4 on `test-engineer` with 3 trials per arm, then decide go or no-go.

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

# Q4: A/B, not run yet
waza run docs/spikes/waza/find-missing-tests/eval.yaml --baseline --trials 3

# Q5: scorer on the fixture (venv with the mutation extra)
uv venv <venv> && uv pip install --python <venv>/bin/python -e '.[mutation]'
<venv>/bin/python -c "from wololo_evals.scoring.mutation import run_mutation_analysis as r; x=r('examples/fixtures/quoting-service/quoting',['examples/fixtures/quoting-service/tests']); print(x.as_dict(), x.kill_rate)"
```

Clone of `ainb-toolkit` used: `/home/claude/.orca/ainb-toolkit` at `a3aaa7e`.
