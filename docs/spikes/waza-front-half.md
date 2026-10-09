# waza front-half spike (slice C)

Question: can `microsoft/waza` run as the front half (execute in Copilot, lint, trigger tests, with/without A/B) with wololo-evals as the back half (mechanical scoring, gate)?

**Recommendation: NO-GO for waza as the front half, for now.** The back half works. The front half is unproven because Copilot auth is blocked on this box.

## Verdicts

| # | Question | Verdict | Evidence (short) |
|---|---|---|---|
| Q1 | Install, Copilot auth | BLOCKED | waza 0.38.9 installed, checksum verified. Copilot returns 403 `not authorized to use this Copilot feature` |
| Q2 | Well-formed (`check`, `spec verify`, `tokens`) | FAIL (waza flags) | find-missing-tests 665/500 tokens; test-driven-development 2213/500; agent file rejected by `waza check`; spec verify 0/1 covered |
| Q3 | Does the skill fire (trigger / skill_invocation) | BLOCKED | No live Copilot run. Casebook scores `qe-test-authoring`, a skill the pack does not ship |
| Q4 | With/without A/B, trials 3 | BLOCKED | Needs live Copilot. No scores computed, none reported |
| Q5 | The join (waza output to mutation scorer) | BLOCKED (scorer side PASS) | Scorer computes kill_rate 0.2373 on the fixture with its own tests. No waza output exists to feed it |

Spend: 0 USD. No waza run reached a model.

## Shape of the join

```
┌───────────────┐  --keep-workspace  ┌──────────────────────┐
│ waza run      │───────────────────▶│ workspace/           │
│ copilot-sdk   │  BLOCKED (Q1)      │ quoting/ tests_gen/  │
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
- Copilot runtime is the embedded CLI `~/.cache/copilot-sdk/1.0.85/copilot`. No `copilot login` has been run. No `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN` in the environment.
- Exact error from `waza models` (the cheapest call that authenticates):

```
Error: failed to list models: JSON-RPC Error -32603: Request models.list failed with message: GenericFailure, {"kind":"http","status":403,"statusText":"Forbidden","body":"unauthorized: not authorized to use this Copilot feature\n", ...}
```

- Unblock (Stevie): authenticate the Copilot CLI for this account (`copilot login`, or set `COPILOT_GITHUB_TOKEN` per `docs/INTEGRATION-DEBT.md:26`), and confirm the account has a Copilot seat. Not worked around with a mock.

## Q2: well-formed

| Target | Command | Flags from waza |
|---|---|---|
| find-missing-tests | `waza check skills/find-missing-tests` | Token budget 665 / 500. Unknown frontmatter `user-invocable`. No eval.yaml. No examples section. Scope signal below minimum |
| test-driven-development | `waza check skills/test-driven-development` | Token budget 2213 / 500. Unknown frontmatter `argument-hint`, `user-invocable`, `version`. No examples section |
| test-engineer agent | `waza check agents/test-engineer.agent.md` | Rejected: `waza check` only accepts skill directories |
| test-engineer agent | `waza tokens count agents/test-engineer.agent.md` | 2026 tokens (no budget applied) |
| find-missing-tests | `waza spec verify <skill> docs/spikes/waza/find-missing-tests/eval.yaml` | 0/1 covered. Uncovered: "create GitHub issues for each gap" |

The 500-token budget is waza's default. Read the flags as "waza's default gate fails", not "the skill is broken".

The spec files validate against waza's published `eval.schema.json` and `task.schema.json` (checked with `jsonschema`). Spec verify's one miss is deliberate: the task forbids filing issues, because the skill would otherwise write to GitHub.

## Q3: does it fire

- Our 0.0833 activation number is not in this repo (no run artifact, no matching value). Source unverified.
- 0.0833 = 1/12, which matches 12 cases with one hit. [inference]
- The casebook asserts `trace:used_skill(qe-test-authoring)` (`benchmarks/qe-skill/cases.toml:31,57`). The pack ships `expect-test`, `find-missing-tests`, `test-driven-development`, `webapp-testing`. `qe-test-authoring` is not in the pack, on any branch (`git log --all -S`), or in GitHub code search. [fact]
- Most likely cause of 0.0833: the scored skill name does not exist, so no shipped skill can satisfy the assertion. Our harness and the casebook are the suspects, not the skill's behaviour. [inference]
- Falsify: rename the casebook assertion to a shipped skill and rerun. Activation moves, or it stays 0.
- Repo copilot adapter has never run live (`docs/INTEGRATION-DEBT.md:26`). [fact]
- Waza measurement is ready: `docs/spikes/waza/find-missing-tests/trigger_tests.yaml` (3 positive, 3 negative) with `inject_skill_body: false`. Unrun.

## Q4: A/B

Blocked. No spec written for the A/B target. Recommended target: the `test-engineer` agent, not find-missing-tests. find-missing-tests files GitHub issues, which is a side effect the A/B should not cause.

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

- **NO-GO** on waza as front half until a live Copilot run works (Q1).
- Back half is ready: the scorer computes a real kill rate; the glue is six items, none a blocker to write.
- Next: unblock Copilot, rerun Q1, Q3, Q4 with the commands below, then decide.

## Reproduce

```bash
# Q1: install (reviewed script; checksum verified by the script)
bash install.sh                       # from raw.githubusercontent.com/microsoft/waza/main
~/bin/waza --version                  # waza version 0.38.9
~/bin/waza models                     # Q1 probe: the 403 above

# Q2: static checks, no model spend
Q=/home/claude/.orca/ainb-toolkit/packages/qe-agent-pack/.apm
waza check $Q/skills/find-missing-tests
waza check $Q/skills/test-driven-development
waza tokens count $Q/agents/test-engineer.agent.md
waza spec verify $Q/skills/find-missing-tests docs/spikes/waza/find-missing-tests/eval.yaml

# Q3 / Q4: blocked, run after auth
waza run docs/spikes/waza/find-missing-tests/eval.yaml -v --keep-workspace
waza run docs/spikes/waza/find-missing-tests/eval.yaml --baseline --trials 3

# Q5: scorer on the fixture (venv with the mutation extra)
uv venv <venv> && uv pip install --python <venv>/bin/python -e '.[mutation]'
<venv>/bin/python -c "from wololo_evals.scoring.mutation import run_mutation_analysis as r; x=r('examples/fixtures/quoting-service/quoting',['examples/fixtures/quoting-service/tests']); print(x.as_dict(), x.kill_rate)"
```

Clone of `ainb-toolkit` used: `/home/claude/.orca/ainb-toolkit` at `a3aaa7e`.
